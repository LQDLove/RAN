from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import numpy as np
import argparse
import urllib
import tarfile
import os
import functools
import itertools
import cv2
import attentive_resnet_2

parser = argparse.ArgumentParser()
parser.add_argument("--model", type=str,
                    default="cifar10_attentive_resnet_2_model", help="model directory")
parser.add_argument("--epochs", type=int, default=50, help="training epochs")
parser.add_argument("--batch", type=int, default=64, help="batch size")
parser.add_argument('--train', action="store_true", help="with training")
parser.add_argument('--eval', action="store_true", help="with evaluation")
parser.add_argument('--predict', action="store_true", help="with prediction")
parser.add_argument('--gpu', type=str, default="0", help="gpu id")
args = parser.parse_args()

tf.logging.set_verbosity(tf.logging.INFO)


def cifar10_input_fn(data_dir, training, num_epochs=1, batch_size=1):

    def download(url, data_dir):

        if not os.path.exists(data_dir):

            os.makedirs(data_dir)

        filename = url.split("/")[-1]
        filepath = os.path.join(data_dir, filename)

        if not os.path.exists(filepath):

            filepath, _ = urllib.request.urlretrieve(url, filepath)

            tarfile.open(filepath).extractall(data_dir)

    def get_filenames(data_dir, training):

        download("https://www.cs.toronto.edu/~kriz/cifar-10-binary.tar.gz", data_dir)

        data_dir = os.path.join(data_dir, "cifar-10-batches-bin")

        if training:

            return [os.path.join(data_dir, "data_batch_{}.bin".format(i)) for i in range(1, 6)]

        else:

            return [os.path.join(data_dir, "test_batch.bin")]

    def preprocess(image, training):

        if training:

            image = tf.image.resize_image_with_crop_or_pad(image, 40, 40)
            image = tf.random_crop(image, (32, 32, 3))
            image = tf.image.random_flip_left_right(image)

        image = tf.image.per_image_standardization(image)

        return image

    def parse(bytes, training):

        record = tf.decode_raw(bytes, tf.uint8)

        image = record[1:]
        image = tf.reshape(image, (3, 32, 32))
        image = tf.transpose(image, (1, 2, 0))
        image = tf.cast(image, tf.float32)
        image = preprocess(image, training)

        label = record[0]
        label = tf.cast(record[0], tf.int32)

        return {"image": image}, label

    filenames = get_filenames(data_dir, training)
    dataset = tf.data.FixedLengthRecordDataset(filenames, 32 * 32 * 3 + 1)
    if training:
        dataset = dataset.shuffle(50000)
    dataset = dataset.repeat(num_epochs)
    dataset = dataset.map(functools.partial(parse, training=training))
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(1)

    return dataset.make_one_shot_iterator().get_next()


def cifar10_model_fn(features, labels, mode, params, channels_first):

    inputs = features["image"]

    if channels_first:

        inputs = tf.transpose(inputs, [0, 3, 1, 2])

    attentive_resnet_model = attentive_resnet_2.Model(
        initial_conv_param=attentive_resnet_2.Model.ConvParam(
            filters=16,
            kernel_size=3,
            strides=1
        ),
        initial_pool_param=attentive_resnet_2.Model.PoolParam(
            pool_size=1,
            strides=1
        ),
        bottleneck=True,
        version=2,
        block_params=[
            attentive_resnet_2.Model.BlockParam(
                blocks=5,
                strides=1
            ),
            attentive_resnet_2.Model.BlockParam(
                blocks=5,
                strides=2
            ),
            attentive_resnet_2.Model.BlockParam(
                blocks=5,
                strides=2
            )
        ],
        attention_block_param=attentive_resnet_2.Model.AttentionBlockParam(
            blocks=1
        ),
        logits_param=attentive_resnet_2.Model.DenseParam(
            units=10
        ),
        channels_first=channels_first
    )

    logits, attentions = attentive_resnet_model(
        inputs=inputs,
        training=mode == tf.estimator.ModeKeys.TRAIN
    )

    if channels_first:

        attentions = tf.transpose(attentions, [0, 2, 3, 1])

    predictions={
        "classes": tf.argmax(
            input=logits,
            axis=1
        ),
        "probabilities": tf.nn.softmax(
            logits=logits,
            name="softmax_tensor"
        ),
        "attentions": attentions
    }

    predictions.update(features)

    if mode == tf.estimator.ModeKeys.PREDICT:

        return tf.estimator.EstimatorSpec(
            mode=mode,
            predictions=predictions
        )

    loss=tf.losses.sparse_softmax_cross_entropy(
        labels=labels,
        logits=logits
    )

    loss += tf.add_n([tf.nn.l2_loss(variable)
                      for variable in tf.trainable_variables()]) * params["weight_decay"]

    if mode == tf.estimator.ModeKeys.EVAL:

        eval_metric_ops={
            "accuracy": tf.metrics.accuracy(
                labels=labels,
                predictions=predictions["classes"]
            )
        }

        return tf.estimator.EstimatorSpec(
            mode=mode,
            loss=loss,
            eval_metric_ops=eval_metric_ops
        )

    if mode == tf.estimator.ModeKeys.TRAIN:

        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):

            optimizer=tf.train.MomentumOptimizer(
                learning_rate=params["learning_rate_fn"](
                    global_step=tf.train.get_global_step()
                ),
                momentum=params["momentum"]
            )

            train_op=optimizer.minimize(
                loss=loss,
                global_step=tf.train.get_global_step()
            )

            return tf.estimator.EstimatorSpec(
                mode=mode,
                loss=loss,
                train_op=train_op
            )


def main(unused_argv):

    cifar10_classifier=tf.estimator.Estimator(
        model_fn=functools.partial(
            cifar10_model_fn,
            channels_first=False
        ),
        model_dir=args.model,
        config=tf.estimator.RunConfig().replace(
            session_config=tf.ConfigProto(
                gpu_options=tf.GPUOptions(
                    visible_device_list=args.gpu
                ),
                device_count={
                    "GPU": 1
                }
            )
        ),
        params={
            "weight_decay": 0.0001,
            "momentum": 0.9,
            "learning_rate_fn": functools.partial(
                tf.train.exponential_decay,
                learning_rate=0.1,
                decay_steps=50000,
                decay_rate=0.1
            )
        }
    )

    if args.train:

        cifar10_classifier.train(
            input_fn=functools.partial(
                cifar10_input_fn,
                data_dir="data",
                training=True,
                num_epochs=args.epochs,
                batch_size=args.batch
            ),
            hooks=[
                tf.train.LoggingTensorHook(
                    tensors={
                        "probabilities": "softmax_tensor"
                    },
                    every_n_iter=100
                )
            ]
        )

    if args.eval:

        eval_results=cifar10_classifier.evaluate(
            input_fn=functools.partial(
                cifar10_input_fn,
                data_dir="data",
                training=False
            )
        )

        print(eval_results)

    if args.predict:

        predict_results=cifar10_classifier.predict(
            input_fn=functools.partial(
                cifar10_input_fn,
                data_dir="data",
                training=False
            )
        )

        for i, predict_result in enumerate(itertools.islice(predict_results, 10)):

            def scale(in_val, in_min, in_max, out_min, out_max):
                return out_min + (in_val - in_min) / (in_max - in_min) * (out_max - out_min)

            image=predict_result["image"]
            attention=predict_result["attentions"]

            image=scale(image, image.min(), image.max(), 0., 255.).astype(np.uint8)
            cv2.imwrite("outputs3/image{}.jpeg".format(i), image)

            attention=np.apply_along_axis(np.sum, -1, attention)[:, :, np.newaxis].repeat(3, -1)
            cv2.imwrite("outputs3/attention{}.jpeg".format(i), attention)


if __name__ == "__main__":

    tf.app.run()