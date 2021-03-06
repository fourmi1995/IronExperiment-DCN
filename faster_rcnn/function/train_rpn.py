# --------------------------------------------------------
# Deformable Convolutional Networks
# Copyright (c) 2017 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Modified by Yuwen Xiong
# --------------------------------------------------------
# Based on:
# MX-RCNN
# Copyright (c) 2016 by Contributors
# Licence under The Apache 2.0 License
# https://github.com/ijkguo/mx-rcnn/
# --------------------------------------------------------

import argparse
import logging
import pprint
import mxnet as mx

from symbols import *
from core import callback, metric
from core.loader import AnchorLoader
from core.module import MutableModule
from utils.load_data import load_gt_roidb, merge_roidb, filter_roidb
from utils.load_model import load_param
from utils.PrefetchingIter import PrefetchingIter
from utils.lr_scheduler import WarmupMultiFactorScheduler


def train_rpn(cfg, dataset, image_set, root_path, dataset_path,
              frequent, kvstore, flip, shuffle, resume,
              ctx, pretrained, epoch, prefix, begin_epoch, end_epoch,
              train_shared, lr, lr_step, logger=None, output_path=None):
    # set up logger
    if not logger:
        logging.basicConfig()
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)

    # set up config
    cfg.TRAIN.BATCH_IMAGES = cfg.TRAIN.ALTERNATE.RPN_BATCH_IMAGES

    # load symbol
    sym_instance = eval(cfg.symbol + '.' + cfg.symbol)()
    sym = sym_instance.get_symbol_rpn(cfg, is_train=True)
    feat_sym = sym.get_internals()['rpn_cls_score_output']

    # setup multi-gpu
    batch_size = len(ctx)
    input_batch_size = cfg.TRAIN.BATCH_IMAGES * batch_size

    # print cfg
    pprint.pprint(cfg)
    logger.info('training rpn cfg:{}\n'.format(pprint.pformat(cfg)))

    # load dataset and prepare imdb for training
    image_sets = [iset for iset in image_set.split('+')]
    roidbs = [load_gt_roidb(dataset, image_set, root_path, dataset_path, result_path=output_path,
                            flip=flip)
              for image_set in image_sets]
    roidb = merge_roidb(roidbs)
    roidb = filter_roidb(roidb, cfg)

    # load training data
    train_data = AnchorLoader(feat_sym, roidb, cfg, batch_size=input_batch_size, shuffle=shuffle,
                              ctx=ctx, feat_stride=cfg.network.RPN_FEAT_STRIDE, anchor_scales=cfg.network.ANCHOR_SCALES,
                              anchor_ratios=cfg.network.ANCHOR_RATIOS, aspect_grouping=cfg.TRAIN.ASPECT_GROUPING)

    # infer max shape
    max_data_shape = [('data', (cfg.TRAIN.BATCH_IMAGES, 3, max([v[0] for v in cfg.SCALES]), max([v[1] for v in cfg.SCALES])))]
    max_data_shape, max_label_shape = train_data.infer_shape(max_data_shape)
    print('providing maximum shape', max_data_shape, max_label_shape)

    # infer shape
    data_shape_dict = dict(train_data.provide_data_single + train_data.provide_label_single)
    sym_instance.infer_shape(data_shape_dict)

    # load and initialize params
    if resume:
        print('continue training from ', begin_epoch)
        arg_params, aux_params = load_param(prefix, begin_epoch, convert=True)
    else:
        arg_params, aux_params = load_param(pretrained, epoch, convert=True)
        sym_instance.init_weight_rpn(cfg, arg_params, aux_params)

    # check parameter shapes
    sym_instance.check_parameter_shapes(arg_params, aux_params, data_shape_dict)

    # create solver
    data_names = [k[0] for k in train_data.provide_data_single]
    label_names = [k[0] for k in train_data.provide_label_single]
    if train_shared:
        fixed_param_prefix = cfg.network.FIXED_PARAMS_SHARED
    else:
        fixed_param_prefix = cfg.network.FIXED_PARAMS
    mod = MutableModule(sym, data_names=data_names, label_names=label_names,
                        logger=logger, context=ctx, max_data_shapes=[max_data_shape for _ in xrange(batch_size)],
                        max_label_shapes=[max_label_shape for _ in xrange(batch_size)], fixed_param_prefix=fixed_param_prefix)

    # decide training params
    # metric
    eval_metric = metric.RPNAccMetric()
    cls_metric = metric.RPNLogLossMetric()
    bbox_metric = metric.RPNL1LossMetric()
    eval_metrics = mx.metric.CompositeEvalMetric()
    for child_metric in [eval_metric, cls_metric, bbox_metric]:
        eval_metrics.add(child_metric)
    # callback
    batch_end_callback = callback.Speedometer(train_data.batch_size, frequent=frequent)
    # epoch_end_callback = mx.callback.do_checkpoint(prefix)
    epoch_end_callback = mx.callback.module_checkpoint(mod, prefix, period=1, save_optimizer_states=True)
    # decide learning rate
    base_lr = lr
    lr_factor = cfg.TRAIN.lr_factor
    lr_epoch = [int(epoch) for epoch in lr_step.split(',')]
    lr_epoch_diff = [epoch - begin_epoch for epoch in lr_epoch if epoch > begin_epoch]
    lr = base_lr * (lr_factor ** (len(lr_epoch) - len(lr_epoch_diff)))
    lr_iters = [int(epoch * len(roidb) / batch_size) for epoch in lr_epoch_diff]
    print('lr', lr, 'lr_epoch_diff', lr_epoch_diff, 'lr_iters', lr_iters)
    lr_scheduler = WarmupMultiFactorScheduler(lr_iters, lr_factor, cfg.TRAIN.warmup, cfg.TRAIN.warmup_lr, cfg.TRAIN.warmup_step)
    # optimizer
    optimizer_params = {'momentum': cfg.TRAIN.momentum,
                        'wd': cfg.TRAIN.wd,
                        'learning_rate': lr,
                        'lr_scheduler': lr_scheduler,
                        'rescale_grad': 1.0,
                        'clip_gradient': None}

    if not isinstance(train_data, PrefetchingIter):
        train_data = PrefetchingIter(train_data)

    # train
    mod.fit(train_data, eval_metric=eval_metrics, epoch_end_callback=epoch_end_callback,
            batch_end_callback=batch_end_callback, kvstore=kvstore,
            optimizer='sgd', optimizer_params=optimizer_params,
            arg_params=arg_params, aux_params=aux_params, begin_epoch=begin_epoch, num_epoch=end_epoch)

