import os
repo_path = os.getenv('MMWAVE_PATH')
import sys
sys.path.append(os.path.join(repo_path, 'models'))
from utils import *
from resnet_amca import ResNetAMCA, AM_logits
import tensorflow as tf
import numpy as np
import argparse
import inspect
import shutil
import yaml
import h5py
from sklearn.metrics import confusion_matrix

def get_parser():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('--epochs', type=int, default=2000)
    parser.add_argument('--init_lr', type=float, default=1e-3)
    parser.add_argument('--num_features', type=int, default=128)
    parser.add_argument('--model_filters', type=int, default=32)
    parser.add_argument('--activation_fn', default='selu')
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--num_classes', type=int, default=10)
    parser.add_argument('--train_source_days', type=int, default=3)
    parser.add_argument('--train_source_unlabeled_days', type=int, default=0)
    parser.add_argument('--train_server_days', type=int, default=1)
    parser.add_argument('--train_conference_days', type=int, default=0)
    parser.add_argument('--save_freq', type=int, default=25)
    parser.add_argument('--log_images_freq', type=int, default=25)
    parser.add_argument('--checkpoint_path', default="checkpoints")
    parser.add_argument('--summary_writer_path', default="tensorboard_logs")
    parser.add_argument('--anneal', type=int, default=4)
    parser.add_argument('--s', type=int, default=10)
    parser.add_argument('--m', type=float, default=0.1)
    parser.add_argument('--ca', type=float, default=1e-3)
    parser.add_argument('--cm_lambda', type=float, default=1e-1)
    parser.add_argument('--orth_lambda', type=float, default=1e-1)
    parser.add_argument('--log_dir', default="logs/Baselines/AMCA_CM/")
    parser.add_argument('--notes', default="AMCA_Orth_Server_Baseline")
    return parser

def save_arg(arg):
    arg_dict = vars(arg)
    if not os.path.exists(arg.log_dir):
        os.makedirs(arg.log_dir)
    with open(os.path.join(arg.log_dir, "config.yaml"), 'w') as f:
        yaml.dump(arg_dict, f)

def get_cross_entropy_loss(labels, logits):
  loss = tf.nn.softmax_cross_entropy_with_logits(labels=labels, logits=logits)
  return tf.reduce_mean(loss)

def get_orth_loss(encodings, labels):
    y, idx, count = tf.unique_with_counts(tf.argmax(labels, axis=-1))
    batch_num_classes = tf.squeeze(tf.shape(y))
    class_centers = tf.zeros((batch_num_classes, num_features), dtype=tf.float32)
    class_centers = tf.tensor_scatter_nd_add(class_centers,
                                             tf.expand_dims(idx, -1),
                                             encodings)
    class_centers /= tf.expand_dims(tf.cast(count, tf.float32), -1)
    batch_orth_loss = tf.matmul(class_centers, class_centers, transpose_b=True)
    batch_orth_loss = tf.reduce_sum(tf.square(batch_orth_loss-tf.eye(batch_num_classes)))
    return batch_orth_loss

@tf.function
def test_step(images):
  logits, _ = model(images, training=False)
  return tf.nn.softmax(logits)

@tf.function
def train_step(src_images, src_labels, srv_images, srv_labels, s, m):
  with tf.GradientTape() as tape:
    src_logits, src_enc = model(src_images, training=True)
    src_logits    = AM_logits(labels=src_labels, logits=src_logits, m=m, s=s)
    batch_cross_entropy_loss  = get_cross_entropy_loss(labels=src_labels,
                                                       logits=src_logits)

    srv_logits, srv_enc = model(srv_images, training=True)
    batch_orth_loss = get_orth_loss(src_enc, src_labels) + \
                      get_orth_loss(srv_enc, tf.nn.softmax(srv_logits))

    cm_src_images, cm_src_labels = cutmix(src_images, src_labels, alpha=1)
    cm_src_logits, _ = model(cm_src_images, training=True)
    batch_cm_cross_entropy_loss  = get_cross_entropy_loss(labels=cm_src_labels,
                                                          logits=cm_src_logits)

    total_loss = batch_cross_entropy_loss + \
                 cm_lambda * batch_cm_cross_entropy_loss + \
                 orth_lambda * batch_orth_loss

  gradients = tape.gradient(total_loss, model.trainable_variables)
  optimizer.apply_gradients(zip(gradients, model.trainable_variables))

  source_train_acc(src_labels, tf.nn.softmax(src_logits))
  cm_cross_entropy_loss(batch_cm_cross_entropy_loss)
  cross_entropy_loss(batch_cross_entropy_loss)
  orth_loss(batch_orth_loss)


if __name__=='__main__':
    parser = get_parser()
    arg = parser.parse_args()

    dataset_path    = os.path.join(repo_path, 'data')
    num_classes     = arg.num_classes
    batch_size      = arg.batch_size
    train_source_days = arg.train_source_days
    train_server_days = arg.train_server_days
    train_conference_days = arg.train_conference_days
    train_source_unlabeled_days = arg.train_source_unlabeled_days
    save_freq       = arg.save_freq
    epochs          = arg.epochs
    init_lr         = arg.init_lr
    num_features    = arg.num_features
    activation_fn   = arg.activation_fn
    model_filters   = arg.model_filters
    anneal          = arg.anneal
    s               = arg.s
    m               = arg.m
    ca              = arg.ca
    log_images_freq = arg.log_images_freq
    cm_lambda       = arg.cm_lambda
    orth_lambda     = arg.orth_lambda

    run_params      = dict(vars(arg))
    del run_params['train_source_unlabeled_days']
    del run_params['train_conference_days']
    del run_params['log_images_freq']
    del run_params['log_dir']
    del run_params['checkpoint_path']
    del run_params['summary_writer_path']
    del run_params['save_freq']
    sorted(run_params)

    run_params      = str(run_params).replace(" ", "").replace("'", "").replace(",", "-")[1:-1]
    log_dir         = os.path.join(repo_path, arg.log_dir, run_params)
    arg.log_dir     = log_dir

    summary_writer_path = os.path.join(log_dir, arg.summary_writer_path)
    checkpoint_path = os.path.join(log_dir, arg.checkpoint_path)

    save_arg(arg)
    shutil.copy2(inspect.getfile(ResNetAMCA), arg.log_dir)
    shutil.copy2(os.path.abspath(__file__), arg.log_dir)

    '''
    Data Preprocessing
    '''

    X_data, y_data, classes = get_h5dataset(os.path.join(dataset_path, 'source_data.h5'))
    print(X_data.shape, y_data.shape, "\n", classes)

    X_data, y_data = balance_dataset(X_data, y_data,
                                     num_days=10,
                                     num_classes=len(classes),
                                     max_samples_per_class=95)
    print(X_data.shape, y_data.shape)

    #split days of data to train and test
    X_src = X_data[y_data[:, 1] < train_source_days]
    y_src = y_data[y_data[:, 1] < train_source_days, 0]
    y_src = np.eye(len(classes))[y_src]
    X_train_src, X_test_src, y_train_src, y_test_src = train_test_split(X_src,
                                                                        y_src,
                                                                        stratify=y_src,
                                                                        test_size=0.10,
                                                                        random_state=42)

    X_trg = X_data[y_data[:, 1] >= train_source_days]
    y_trg = y_data[y_data[:, 1] >= train_source_days]
    X_train_trg = X_trg[y_trg[:, 1] < train_source_days+train_source_unlabeled_days]
    y_train_trg = y_trg[y_trg[:, 1] < train_source_days+train_source_unlabeled_days, 0]
    y_train_trg = np.eye(len(classes))[y_train_trg]

    X_test_trg = X_data[y_data[:, 1] >= train_source_days+train_source_unlabeled_days]
    y_test_trg = y_data[y_data[:, 1] >= train_source_days+train_source_unlabeled_days, 0]
    y_test_trg = np.eye(len(classes))[y_test_trg]

    del X_src, y_src, X_trg, y_trg, X_data, y_data

    #mean center and normalize dataset
    X_train_src, src_mean = mean_center(X_train_src)
    X_train_src, src_min, src_ptp = normalize(X_train_src)

    X_test_src, _    = mean_center(X_test_src, src_mean)
    X_test_src, _, _ = normalize(X_test_src, src_min, src_ptp)

    if(X_train_trg.shape[0] != 0):
      X_train_trg, trg_mean = mean_center(X_train_trg)
      X_train_trg, trg_min, trg_ptp = normalize(X_train_trg)

      X_test_trg, _    = mean_center(X_test_trg, trg_mean)
      X_test_trg, _, _ = normalize(X_test_trg, trg_min, trg_ptp)
    else:
      X_test_trg, _    = mean_center(X_test_trg, src_mean)
      X_test_trg, _, _ = normalize(X_test_trg, src_min, src_ptp)

    X_train_src = X_train_src.astype(np.float32)
    y_train_src = y_train_src.astype(np.uint8)
    X_test_src  = X_test_src.astype(np.float32)
    y_test_src  = y_test_src.astype(np.uint8)
    X_train_trg = X_train_trg.astype(np.float32)
    y_train_trg = y_train_trg.astype(np.uint8)
    X_test_trg  = X_test_trg.astype(np.float32)
    y_test_trg  = y_test_trg.astype(np.uint8)
    print("Final shapes: ")
    print(X_train_src.shape, y_train_src.shape,  X_test_src.shape, \
          y_test_src.shape, X_train_trg.shape, y_train_trg.shape, \
          X_test_trg.shape, y_test_trg.shape)

    X_train_conf,   y_train_conf,   X_test_conf,   y_test_conf   = get_trg_data(os.path.join(dataset_path,
                                                                                             'target_conf_data.h5'),
                                                                                classes,
                                                                                train_conference_days)
    X_train_server, y_train_server, X_test_server, y_test_server = get_trg_data(os.path.join(dataset_path,
                                                                                             'target_server_data.h5'),
                                                                                classes,
                                                                                train_server_days)
    _             , _             , X_data_office, y_data_office = get_trg_data(os.path.join(dataset_path,
                                                                                             'target_office_data.h5'),
                                                                                classes,
                                                                                0)

    print(X_train_conf.shape, y_train_conf.shape,
          X_test_conf.shape, y_test_conf.shape, "\n",
          X_train_server.shape, y_train_server.shape,
          X_test_server.shape, y_test_server.shape, "\n",
          X_data_office.shape,  y_data_office.shape)

    #get tf.data objects for each set
    #Test
    conf_test_set = tf.data.Dataset.from_tensor_slices((X_test_conf, y_test_conf))
    conf_test_set = conf_test_set.batch(batch_size, drop_remainder=False)
    conf_test_set = conf_test_set.prefetch(batch_size)

    server_test_set = tf.data.Dataset.from_tensor_slices((X_test_server, y_test_server))
    server_test_set = server_test_set.batch(batch_size, drop_remainder=False)
    server_test_set = server_test_set.prefetch(batch_size)

    office_test_set = tf.data.Dataset.from_tensor_slices((X_data_office, y_data_office))
    office_test_set = office_test_set.batch(batch_size, drop_remainder=False)
    office_test_set = office_test_set.prefetch(batch_size)

    src_test_set = tf.data.Dataset.from_tensor_slices((X_test_src, y_test_src))
    src_test_set = src_test_set.batch(batch_size, drop_remainder=False)
    src_test_set = src_test_set.prefetch(batch_size)

    time_test_set = tf.data.Dataset.from_tensor_slices((X_test_trg, y_test_trg))
    time_test_set = time_test_set.batch(batch_size, drop_remainder=False)
    time_test_set = time_test_set.prefetch(batch_size)

    #Train
    src_train_set = tf.data.Dataset.from_tensor_slices((X_train_src, y_train_src))
    src_train_set = src_train_set.shuffle(X_train_src.shape[0])
    src_train_set = src_train_set.batch(batch_size, drop_remainder=True)
    src_train_set = src_train_set.prefetch(batch_size)

    server_train_set = tf.data.Dataset.from_tensor_slices((X_train_server, y_train_server))
    server_train_set = server_train_set.shuffle(X_train_server.shape[0])
    server_train_set = server_train_set.batch(batch_size, drop_remainder=True)
    server_train_set = server_train_set.prefetch(batch_size)

    '''
    Tensorflow Model
    '''

    source_train_acc      = tf.keras.metrics.CategoricalAccuracy(name='source_train_acc')
    source_test_acc       = tf.keras.metrics.CategoricalAccuracy(name='source_test_acc')
    temporal_test_acc     = tf.keras.metrics.CategoricalAccuracy(name='temporal_test_acc')
    office_test_acc       = tf.keras.metrics.CategoricalAccuracy(name='office_test_acc')
    server_train_acc      = tf.keras.metrics.CategoricalAccuracy(name='server_train_acc')
    server_test_acc       = tf.keras.metrics.CategoricalAccuracy(name='server_test_acc')
    conference_train_acc  = tf.keras.metrics.CategoricalAccuracy(name='conference_train_acc')
    conference_test_acc   = tf.keras.metrics.CategoricalAccuracy(name='conference_test_acc')
    cross_entropy_loss    = tf.keras.metrics.Mean(name='cross_entropy_loss')
    cm_cross_entropy_loss = tf.keras.metrics.Mean(name='cm_cross_entropy_loss')
    orth_loss             = tf.keras.metrics.Mean(name='orth_loss')

    learning_rate  = tf.keras.optimizers.schedules.PolynomialDecay(init_lr,
                                                                   decay_steps=(X_train_src.shape[0]//batch_size)*200,
                                                                   end_learning_rate=init_lr*1e-2,
                                                                   cycle=True)
    model      = ResNetAMCA(num_classes,
                            num_features,
                            num_filters=model_filters,
                            activation=activation_fn,
                            ca_decay=ca)
    optimizer  = tf.keras.optimizers.Adam(learning_rate=learning_rate)

    summary_writer = tf.summary.create_file_writer(summary_writer_path)
    ckpt           = tf.train.Checkpoint(model=model,
                                         optimizer=optimizer)
    ckpt_manager   = tf.train.CheckpointManager(ckpt,
                                                checkpoint_path,
                                                max_to_keep=5)

    m_anneal = tf.Variable(0, dtype="float32")
    for epoch in range(epochs):
      m_anneal.assign(tf.minimum(m*(epoch/(epochs/anneal)), m))
      for source_data, server_data in zip(src_train_set, server_train_set):
        train_step(source_data[0], source_data[1],
                   server_data[0], server_data[1], s, m_anneal)

      pred_labels = []
      for data in time_test_set:
        pred_labels.extend(test_step(data[0]))
      temporal_test_acc(pred_labels, y_test_trg)
      if (epoch + 1) % log_images_freq == 0:
          cm = confusion_matrix(np.argmax(y_test_trg, axis=-1), np.argmax(pred_labels, axis=-1))
          cm_image = plot_to_image(plot_confusion_matrix(cm, class_names=classes))
          with summary_writer.as_default():
            tf.summary.image("Temporal Test Confusion Matrix", cm_image, step=epoch)

      pred_labels = []
      for data in src_test_set:
        pred_labels.extend(test_step(data[0]))
      source_test_acc(pred_labels, y_test_src)
      if (epoch + 1) % log_images_freq == 0:
          cm = confusion_matrix(np.argmax(y_test_src, axis=-1), np.argmax(pred_labels, axis=-1))
          cm_image = plot_to_image(plot_confusion_matrix(cm, class_names=classes))
          with summary_writer.as_default():
            tf.summary.image("Source Test Confusion Matrix", cm_image, step=epoch)

      pred_labels = []
      for data in office_test_set:
        pred_labels.extend(test_step(data[0]))
      office_test_acc(pred_labels, y_data_office)
      if (epoch + 1) % log_images_freq == 0:
          cm = confusion_matrix(np.argmax(y_data_office, axis=-1), np.argmax(pred_labels, axis=-1))
          cm_image = plot_to_image(plot_confusion_matrix(cm, class_names=classes))
          with summary_writer.as_default():
            tf.summary.image("Office Test Confusion Matrix", cm_image, step=epoch)

      pred_labels = []
      for data in server_test_set:
        pred_labels.extend(test_step(data[0]))
      server_test_acc(pred_labels, y_test_server)
      if (epoch + 1) % log_images_freq == 0:
          cm = confusion_matrix(np.argmax(y_test_server, axis=-1), np.argmax(pred_labels, axis=-1))
          cm_image = plot_to_image(plot_confusion_matrix(cm, class_names=classes))
          with summary_writer.as_default():
            tf.summary.image("Server Test Confusion Matrix", cm_image, step=epoch)

      pred_labels = []
      for data in conf_test_set:
        pred_labels.extend(test_step(data[0]))
      conference_test_acc(pred_labels, y_test_conf)
      if (epoch + 1) % log_images_freq == 0:
          cm = confusion_matrix(np.argmax(y_test_conf, axis=-1), np.argmax(pred_labels, axis=-1))
          cm_image = plot_to_image(plot_confusion_matrix(cm, class_names=classes))
          with summary_writer.as_default():
            tf.summary.image("Conference Test Confusion Matrix", cm_image, step=epoch)

      with summary_writer.as_default():
        tf.summary.scalar("temporal_test_acc", temporal_test_acc.result(), step=epoch)
        tf.summary.scalar("source_train_acc", source_train_acc.result(), step=epoch)
        tf.summary.scalar("source_test_acc", source_test_acc.result(), step=epoch)
        tf.summary.scalar("office_test_acc", office_test_acc.result(), step=epoch)
        tf.summary.scalar("server_test_acc", server_test_acc.result(), step=epoch)
        tf.summary.scalar("conference_test_acc", conference_test_acc.result(), step=epoch)
        tf.summary.scalar("cross_entropy_loss", cross_entropy_loss.result(), step=epoch)
        tf.summary.scalar("cm_cross_entropy_loss", cm_cross_entropy_loss.result(), step=epoch)
        tf.summary.scalar("orth_loss", orth_loss.result(), step=epoch)

      if (epoch + 1) % save_freq == 0:
        ckpt_save_path = ckpt_manager.save()
        print ('Saved checkpoint for epoch {} at {}'.format(epoch+1,
                                                             ckpt_save_path))

      temporal_test_acc.reset_states()
      source_train_acc.reset_states()
      source_test_acc.reset_states()
      office_test_acc.reset_states()
      server_test_acc.reset_states()
      conference_test_acc.reset_states()
      cross_entropy_loss.reset_states()
      cm_cross_entropy_loss.reset_states()
      orth_loss.reset_states()

    if save_freq != 0:
      ckpt_save_path = ckpt_manager.save()
      print('Saved final checkpoint at {}'.format(ckpt_save_path))