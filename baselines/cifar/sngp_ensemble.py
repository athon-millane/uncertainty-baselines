# coding=utf-8
# Copyright 2020 The Uncertainty Baselines Authors.
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

"""Ensemble of SNGP models on CIFAR.

This script only performs evaluation, not training. We recommend training
ensembles by launching independent runs of `sngp.py` over different
seeds.
"""

import functools
import os

from absl import app
from absl import flags
from absl import logging

import edward2 as ed
import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
import uncertainty_baselines as ub
import utils  # local file import
import uncertainty_metrics as um

flags.DEFINE_string('checkpoint_dir', None,
                    'The directory where the model weights are stored.')
flags.mark_flag_as_required('checkpoint_dir')
flags.DEFINE_integer('seed', 42, 'Random seed.')
flags.DEFINE_integer('per_core_batch_size', 64, 'Batch size per TPU core/GPU.')
flags.DEFINE_enum('dataset', 'cifar10',
                  enum_values=['cifar10', 'cifar100'],
                  help='Dataset.')
# TODO(ghassen): consider adding CIFAR-100-C to TFDS.
flags.DEFINE_string('cifar100_c_path', None,
                    'Path to the TFRecords files for CIFAR-100-C. Only valid '
                    '(and required) if dataset is cifar100 and corruptions.')
flags.DEFINE_integer('num_bins', 15, 'Number of bins for ECE.')
flags.DEFINE_string('output_dir', '/tmp/cifar', 'Output directory.')

# SNGP ensemble flags
flags.DEFINE_float(
    'gp_mean_field_factor_ensemble', 0.0005,
    'The tunable multiplicative factor used in the mean-field approximation '
    'for the posterior mean of softmax Gaussian process. If -1 then use '
    'posterior mode instead of posterior mean.')

# Dropout flags
flags.DEFINE_bool('use_filterwise_dropout', True,
                  'Whether to use filterwise dropout for the hidden layers.')
flags.DEFINE_bool('use_mc_dropout', False,
                  'Whether to use Monte Carlo dropout for the hidden layers.')
flags.DEFINE_float('dropout_rate', 0.1, 'Dropout rate.')

# SNGP flags.
flags.DEFINE_bool('use_spec_norm', True,
                  'Whether to apply spectral normalization.')
flags.DEFINE_bool('use_gp_layer', True,
                  'Whether to use Gaussian process as the output layer.')

# Spectral normalization flags.
flags.DEFINE_integer(
    'spec_norm_iteration', 1,
    'Number of power iterations to perform for estimating '
    'the spectral norm of weight matrices.')
flags.DEFINE_float('spec_norm_bound', 6.,
                   'Upper bound to spectral norm of weight matrices.')

# Gaussian process flags.
flags.DEFINE_float('gp_bias', 0., 'The bias term for GP layer.')
flags.DEFINE_float(
    'gp_scale', 2.,
    'The length-scale parameter for the RBF kernel of the GP layer.')
flags.DEFINE_integer(
    'gp_input_dim', 128,
    'The dimension to reduce the neural network input for the GP layer '
    '(via random Gaussian projection which preserves distance by the '
    ' Johnson-Lindenstrauss lemma). If -1, no dimension reduction.')
flags.DEFINE_integer(
    'gp_hidden_dim', 1024,
    'The hidden dimension of the GP layer, which corresponds to the number of '
    'random features used for the approximation.')
flags.DEFINE_bool(
    'gp_input_normalization', True,
    'Whether to normalize the input using LayerNorm for GP layer.'
    'This is similar to automatic relevance determination (ARD) in the classic '
    'GP learning.')
flags.DEFINE_string(
    'gp_random_feature_type', 'orf',
    'The type of random feature to use. One of "rff" (random fourier feature), '
    '"orf" (orthogonal random feature).')
flags.DEFINE_float('gp_cov_ridge_penalty', 1.,
                   'Ridge penalty parameter for GP posterior covariance.')
flags.DEFINE_float(
    'gp_cov_discount_factor', -1.,
    'The discount factor to compute the moving average of precision matrix'
    'across epochs. If -1 then compute the exact precision matrix within the '
    'latest epoch.')

# Accelerator flags.
flags.DEFINE_bool('use_gpu', True, 'Whether to run on GPU or otherwise TPU.')
flags.DEFINE_bool('use_bfloat16', False, 'Whether to use mixed precision.')
flags.DEFINE_integer('num_cores', 1, 'Number of TPU cores or number of GPUs.')
FLAGS = flags.FLAGS


def main(argv):
  del argv  # unused arg
  if not FLAGS.use_gpu:
    raise ValueError('Only GPU is currently supported.')
  if FLAGS.num_cores > 1:
    raise ValueError('Only a single accelerator is currently supported.')
  tf.random.set_seed(FLAGS.seed)
  tf.io.gfile.makedirs(FLAGS.output_dir)

  ds_info = tfds.builder(FLAGS.dataset).info
  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  steps_per_eval = ds_info.splits['test'].num_examples // batch_size
  num_classes = ds_info.features['label'].num_classes

  dataset = utils.load_dataset(
      split=tfds.Split.TEST,
      name=FLAGS.dataset,
      batch_size=batch_size,
      use_bfloat16=FLAGS.use_bfloat16)
  test_datasets = {'clean': dataset}
  corruption_types, max_intensity = utils.load_corrupted_test_info(
      FLAGS.dataset)
  for name in corruption_types:
    for intensity in range(1, max_intensity + 1):
      dataset_name = '{0}_{1}'.format(name, intensity)
      if FLAGS.dataset == 'cifar10':
        load_c_dataset = utils.load_cifar10_c
      else:
        load_c_dataset = functools.partial(utils.load_cifar100_c,
                                           path=FLAGS.cifar100_c_path)
      dataset = load_c_dataset(
          corruption_name=name,
          corruption_intensity=intensity,
          batch_size=batch_size,
          use_bfloat16=FLAGS.use_bfloat16)
      test_datasets[dataset_name] = dataset

  model = ub.models.wide_resnet_sngp(
      input_shape=ds_info.features['image'].shape,
      batch_size=FLAGS.per_core_batch_size,
      depth=28,
      width_multiplier=10,
      num_classes=num_classes,
      l2=0.,
      use_mc_dropout=FLAGS.use_mc_dropout,
      use_filterwise_dropout=FLAGS.use_filterwise_dropout,
      dropout_rate=FLAGS.dropout_rate,
      use_gp_layer=FLAGS.use_gp_layer,
      gp_input_dim=FLAGS.gp_input_dim,
      gp_hidden_dim=FLAGS.gp_hidden_dim,
      gp_scale=FLAGS.gp_scale,
      gp_bias=FLAGS.gp_bias,
      gp_input_normalization=FLAGS.gp_input_normalization,
      gp_random_feature_type=FLAGS.gp_random_feature_type,
      gp_cov_discount_factor=FLAGS.gp_cov_discount_factor,
      gp_cov_ridge_penalty=FLAGS.gp_cov_ridge_penalty,
      use_spec_norm=FLAGS.use_spec_norm,
      spec_norm_iteration=FLAGS.spec_norm_iteration,
      spec_norm_bound=FLAGS.spec_norm_bound)
  logging.info('Model input shape: %s', model.input_shape)
  logging.info('Model output shape: %s', model.output_shape)
  logging.info('Model number of weights: %s', model.count_params())

  # Search for checkpoints from their index file; then remove the index suffix.
  ensemble_filenames = tf.io.gfile.glob(os.path.join(FLAGS.checkpoint_dir,
                                                     '**/*.index'))
  ensemble_filenames = [filename[:-6] for filename in ensemble_filenames]
  ensemble_size = len(ensemble_filenames)
  logging.info('Ensemble size: %s', ensemble_size)
  logging.info('Ensemble number of weights: %s',
               ensemble_size * model.count_params())
  logging.info('Ensemble filenames: %s', str(ensemble_filenames))
  checkpoint = tf.train.Checkpoint(model=model)

  # Write model predictions to files.
  num_datasets = len(test_datasets)
  for m, ensemble_filename in enumerate(ensemble_filenames):
    checkpoint.restore(ensemble_filename)
    for n, (name, test_dataset) in enumerate(test_datasets.items()):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      if not tf.io.gfile.exists(filename):
        logits = []
        test_iterator = iter(test_dataset)
        for _ in range(steps_per_eval):
          features, _ = next(test_iterator)  # pytype: disable=attribute-error
          logits_member = model(features, training=False)
          if isinstance(logits_member, tuple):
            # If model returns a tuple of (logits, covmat), extract both
            logits_member, covmat_member = logits_member
          else:
            covmat_member = tf.eye(FLAGS.per_core_batch_size)
          logits_member = ed.layers.utils.mean_field_logits(
              logits_member, covmat_member, FLAGS.gp_mean_field_factor_ensemble)
          logits.append(logits_member)

        logits = tf.concat(logits, axis=0)
        with tf.io.gfile.GFile(filename, 'w') as f:
          np.save(f, logits.numpy())
      percent = (m * num_datasets + (n + 1)) / (ensemble_size * num_datasets)
      message = ('{:.1%} completion for prediction: ensemble member {:d}/{:d}. '
                 'Dataset {:d}/{:d}'.format(percent,
                                            m + 1,
                                            ensemble_size,
                                            n + 1,
                                            num_datasets))
      logging.info(message)

  metrics = {
      'test/negative_log_likelihood': tf.keras.metrics.Mean(),
      'test/gibbs_cross_entropy': tf.keras.metrics.Mean(),
      'test/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
      'test/ece': um.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
  }
  corrupt_metrics = {}
  for name in test_datasets:
    corrupt_metrics['test/nll_{}'.format(name)] = tf.keras.metrics.Mean()
    corrupt_metrics['test/accuracy_{}'.format(name)] = (
        tf.keras.metrics.SparseCategoricalAccuracy())
    corrupt_metrics['test/ece_{}'.format(name)] = (
        um.ExpectedCalibrationError(num_bins=FLAGS.num_bins))

  # Evaluate model predictions.
  for n, (name, test_dataset) in enumerate(test_datasets.items()):
    logits_dataset = []
    for m in range(ensemble_size):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      with tf.io.gfile.GFile(filename, 'rb') as f:
        logits_dataset.append(np.load(f))

    logits_dataset = tf.convert_to_tensor(logits_dataset)
    test_iterator = iter(test_dataset)
    for step in range(steps_per_eval):
      _, labels = next(test_iterator)  # pytype: disable=attribute-error
      logits = logits_dataset[:, (step*batch_size):((step+1)*batch_size)]
      labels = tf.cast(labels, tf.int32)
      negative_log_likelihood = um.ensemble_cross_entropy(labels, logits)
      per_probs = tf.nn.softmax(logits)
      probs = tf.reduce_mean(per_probs, axis=0)
      if name == 'clean':
        gibbs_ce = um.gibbs_cross_entropy(labels, logits)
        metrics['test/negative_log_likelihood'].update_state(
            negative_log_likelihood)
        metrics['test/gibbs_cross_entropy'].update_state(gibbs_ce)
        metrics['test/accuracy'].update_state(labels, probs)
        metrics['test/ece'].update_state(labels, probs)
      else:
        corrupt_metrics['test/nll_{}'.format(name)].update_state(
            negative_log_likelihood)
        corrupt_metrics['test/accuracy_{}'.format(name)].update_state(
            labels, probs)
        corrupt_metrics['test/ece_{}'.format(name)].update_state(
            labels, probs)

    message = ('{:.1%} completion for evaluation: dataset {:d}/{:d}'.format(
        (n + 1) / num_datasets, n + 1, num_datasets))
    logging.info(message)

  corrupt_results = utils.aggregate_corrupt_metrics(corrupt_metrics,
                                                    corruption_types,
                                                    max_intensity)
  total_results = {name: metric.result() for name, metric in metrics.items()}
  total_results.update(corrupt_results)
  logging.info('Metrics: %s', total_results)


if __name__ == '__main__':
  app.run(main)
