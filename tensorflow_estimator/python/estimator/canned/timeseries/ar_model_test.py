# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for ar_model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import math

from tensorflow.python.client import session
from tensorflow.python.data.ops import dataset_ops
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import test_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import variable_scope
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.platform import tf_logging as logging
from tensorflow_estimator.python.estimator import estimator_lib
from tensorflow_estimator.python.estimator.canned.timeseries import ar_model
from tensorflow_estimator.python.estimator.canned.timeseries.estimators import LSTMAutoRegressor
from tensorflow_estimator.python.estimator.canned.timeseries.feature_keys import PredictionFeatures
from tensorflow_estimator.python.estimator.canned.timeseries.feature_keys import TrainEvalFeatures


class InputFnBuilder(object):

  def __init__(self,
               noise_stddev,
               periods,
               window_size,
               batch_size,
               num_samples=200):
    self.window_size = window_size
    self.batch_size = batch_size

    split = int(num_samples * 0.8)
    self.initialize_data = lambda: self.initialize_data_with_properties(
        noise_stddev, periods, num_samples, split)

  def initialize_data_with_properties(self, noise_stddev, periods, num_samples,
                                      split):
    time = 1 + 3 * math_ops.range(num_samples, dtype=dtypes.int64)
    time_offset = 2 * math.pi * math_ops.cast(time % periods[0],
                                              dtypes.float32) / periods[0]
    time_offset = time_offset[:, None]
    if len(periods) > 1:
      time_offset2 = math_ops.cast(time % periods[1],
                                   dtypes.float32) / periods[1]
      time_offset2 = time_offset2[:, None]
      data1 = math_ops.sin(time_offset / 2.0)**2 * (1 + time_offset2)
    else:
      data1 = math_ops.sin(2 * time_offset) + math_ops.cos(3 * time_offset)
    data1_noise = noise_stddev / 4. * random_ops.random_normal([num_samples],
                                                               1)[:, None]
    data1 = math_ops.add(data1, data1_noise)

    data2 = math_ops.sin(3 * time_offset) + math_ops.cos(5 * time_offset)
    data2_noise = noise_stddev / 3. * random_ops.random_normal([num_samples],
                                                               1)[:, None]
    data2 = math_ops.add(data2, data2_noise)
    data = array_ops.concat((4 * data1, 3 * data2), 1)
    self.train_data, self.test_data = data[0:split], data[split:]
    self.train_time, self.test_time = time[0:split], time[split:]

  def train_or_test_input_fn(self, time, data):

    def map_to_dict(time, data):
      return {TrainEvalFeatures.TIMES: time, TrainEvalFeatures.VALUES: data}

    def batch_windows(time, data):
      return dataset_ops.Dataset.zip((time, data)).batch(
          self.window_size, drop_remainder=True)

    dataset = dataset_ops.Dataset.from_tensor_slices((time, data))
    dataset = dataset.window(self.window_size, shift=1, drop_remainder=True)
    dataset = dataset.shuffle(1000, seed=2).repeat()
    dataset = dataset.flat_map(batch_windows).batch(
        self.batch_size).map(map_to_dict)
    return dataset

  def train_input_fn(self):
    self.initialize_data()
    return self.train_or_test_input_fn(self.train_time, self.train_data)

  def test_input_fn(self):
    self.initialize_data()
    return self.train_or_test_input_fn(self.test_time, self.test_data)

  def prediction_input_fn(self):

    def map_to_dict(predict_times, predict_true_values, state_times,
                    state_values, state_exogenous):
      return ({
          PredictionFeatures.TIMES:
              predict_times[None, :],
          TrainEvalFeatures.VALUES:
              predict_true_values[None, :],
          PredictionFeatures.STATE_TUPLE: (state_times[None, :],
                                           state_values[None, :],
                                           state_exogenous[None, :])
      }, {})

    self.initialize_data()
    predict_times = array_ops.concat(
        [self.train_time[self.window_size:], self.test_time], 0)[None, :]
    predict_true_values = array_ops.concat(
        [self.train_data[self.window_size:], self.test_data], 0)[None, :]
    state_times = math_ops.cast(self.train_time[:self.window_size][None, :],
                                dtypes.float32)
    state_values = math_ops.cast(self.train_data[:self.window_size, :][None, :],
                                 dtypes.float32)
    state_exogenous = state_times[:, :, None][:, :, :0]

    dataset = dataset_ops.Dataset.from_tensor_slices(
        (predict_times, predict_true_values, state_times, state_values,
         state_exogenous))
    dataset = dataset.map(map_to_dict)
    return dataset

  def true_values(self):
    self.initialize_data()
    predict_true_values = array_ops.concat(
        [self.train_data[self.window_size:], self.test_data], 0)[None, :]
    true_values = predict_true_values[0, :, 0]
    return true_values


@test_util.run_v1_only("Currently incompatible with ResourceVariable")
class ARModelTest(test.TestCase):

  def train_helper(self, input_window_size, loss, max_loss=None, periods=(25,)):
    data_noise_stddev = 0.2
    if max_loss is None:
      if loss == ar_model.ARModel.NORMAL_LIKELIHOOD_LOSS:
        max_loss = 1.0
      else:
        max_loss = 0.05 / (data_noise_stddev**2)
    output_window_size = 10
    window_size = input_window_size + output_window_size
    input_fn_builder = InputFnBuilder(
        noise_stddev=data_noise_stddev,
        periods=periods,
        window_size=window_size,
        batch_size=64)

    class _RunConfig(estimator_lib.RunConfig):

      @property
      def tf_random_seed(self):
        return 3

    estimator = LSTMAutoRegressor(
        periodicities=periods,
        input_window_size=input_window_size,
        output_window_size=output_window_size,
        num_features=2,
        num_timesteps=20,
        num_units=16,
        loss=loss,
        config=_RunConfig())

    # Test training
    # Note that most models will require many more steps to fully converge. We
    # have used a small number of steps here to keep the running time small.
    estimator.train(input_fn=input_fn_builder.train_input_fn, steps=300)
    test_evaluation = estimator.evaluate(
        input_fn=input_fn_builder.test_input_fn, steps=1)
    test_loss = test_evaluation["loss"]
    logging.warning("Final test loss: %f", test_loss)
    self.assertLess(test_loss, max_loss)
    if loss == ar_model.ARModel.SQUARED_LOSS:
      # Test that the evaluation loss is reported without input scaling.
      self.assertAllClose(
          test_loss,
          math_ops.reduce_mean(
              (test_evaluation["mean"] - test_evaluation["observed"])**2))

    # Test predict
    (predictions,) = tuple(
        estimator.predict(input_fn=input_fn_builder.prediction_input_fn))
    predicted_mean = predictions["mean"][:, 0]

    if loss == ar_model.ARModel.NORMAL_LIKELIHOOD_LOSS:
      variances = predictions["covariance"][:, 0]
      standard_deviations = math_ops.sqrt(variances)
      # Note that we may get tighter bounds with more training steps.
      true_values = input_fn_builder.true_values()
      errors = math_ops.abs(predicted_mean -
                            true_values) > 4 * standard_deviations
      fraction_errors = math_ops.reduce_mean(
          math_ops.cast(errors, dtypes.float32))
      logging.warning("Fraction errors: %f", self.evaluate(fraction_errors))

  def test_autoregression_squared(self):
    self.train_helper(input_window_size=15,
                      loss=ar_model.ARModel.SQUARED_LOSS)

  def test_autoregression_short_input_window(self):
    self.train_helper(input_window_size=8,
                      loss=ar_model.ARModel.SQUARED_LOSS)

  def test_autoregression_normal(self):
    self.train_helper(
        input_window_size=10,
        loss=ar_model.ARModel.NORMAL_LIKELIHOOD_LOSS,
        max_loss=50.)  # Just make sure there are no exceptions.

  def test_autoregression_normal_multiple_periods(self):
    self.train_helper(
        input_window_size=10,
        loss=ar_model.ARModel.NORMAL_LIKELIHOOD_LOSS,
        max_loss=2.0,
        periods=(25, 55))

  def test_wrong_window_size(self):
    estimator = LSTMAutoRegressor(
        periodicities=10,
        input_window_size=10,
        output_window_size=6,
        num_features=1)

    def _bad_window_size_input_fn():
      return ({TrainEvalFeatures.TIMES: [[1]],
               TrainEvalFeatures.VALUES: [[[1.]]]},
              None)
    def _good_data():
      return ({
          TrainEvalFeatures.TIMES:
              math_ops.range(16)[None, :],
          TrainEvalFeatures.VALUES:
              array_ops.reshape(math_ops.range(16), [1, 16, 1])
      }, None)

    with self.assertRaisesRegexp(ValueError, "set window_size=16"):
      estimator.train(input_fn=_bad_window_size_input_fn, steps=1)
    # Get a checkpoint for evaluation
    estimator.train(input_fn=_good_data, steps=1)
    with self.assertRaisesRegexp(ValueError, "requires a window of at least"):
      estimator.evaluate(input_fn=_bad_window_size_input_fn, steps=1)

  def test_predictions_direct_lstm(self):
    model = ar_model.ARModel(periodicities=2,
                             num_features=1,
                             num_time_buckets=10,
                             input_window_size=2,
                             output_window_size=2,
                             prediction_model_factory=functools.partial(
                                 ar_model.LSTMPredictionModel,
                                 num_units=16))
    with session.Session():
      predicted_values = model.predict({
          PredictionFeatures.TIMES: [[4, 6, 10]],
          PredictionFeatures.STATE_TUPLE: (
              [[1, 2]], [[[1.], [2.]]], [[[], []]])
      })
      variables.global_variables_initializer().run()
      self.assertAllEqual(predicted_values["mean"].eval().shape,
                          [1, 3, 1])

  def test_long_eval(self):
    model = ar_model.ARModel(periodicities=2,
                             num_features=1,
                             num_time_buckets=10,
                             input_window_size=2,
                             output_window_size=1)
    raw_features = {
        TrainEvalFeatures.TIMES: [[1, 3, 5, 7, 11]],
        TrainEvalFeatures.VALUES: [[[1.], [2.], [3.], [4.], [5.]]]}
    model.initialize_graph()
    with variable_scope.variable_scope("armodel"):
      raw_evaluation = model.define_loss(
          raw_features, mode=estimator_lib.ModeKeys.EVAL)
    with session.Session() as sess:
      variables.global_variables_initializer().run()
      raw_evaluation_evaled = sess.run(raw_evaluation)
      self.assertAllEqual([[5, 7, 11]],
                          raw_evaluation_evaled.prediction_times)
      for feature_name in raw_evaluation.predictions:
        self.assertAllEqual(
            [1, 3, 1],  # batch, window, num_features. The window size has 2
                        # cut off for the first input_window.
            raw_evaluation_evaled.predictions[feature_name].shape)

  def test_long_eval_discard_indivisible(self):
    model = ar_model.ARModel(periodicities=2,
                             num_features=1,
                             num_time_buckets=10,
                             input_window_size=2,
                             output_window_size=2)
    raw_features = {
        TrainEvalFeatures.TIMES: [[1, 3, 5, 7, 11]],
        TrainEvalFeatures.VALUES: [[[1.], [2.], [3.], [4.], [5.]]]}
    model.initialize_graph()
    raw_evaluation = model.define_loss(
        raw_features, mode=estimator_lib.ModeKeys.EVAL)
    with session.Session() as sess:
      variables.global_variables_initializer().run()
      raw_evaluation_evaled = sess.run(raw_evaluation)
      self.assertAllEqual([[7, 11]],
                          raw_evaluation_evaled.prediction_times)
      for feature_name in raw_evaluation.predictions:
        self.assertAllEqual(
            [1, 2, 1],  # batch, window, num_features. The window has two cut
                        # off for the first input window and one discarded so
                        # that the remainder is divisible into output windows.
            raw_evaluation_evaled.predictions[feature_name].shape)


if __name__ == "__main__":
  test.main()
