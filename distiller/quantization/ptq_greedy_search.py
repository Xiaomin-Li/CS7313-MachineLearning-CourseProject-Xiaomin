#
# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
r"""
Here we implement the greedy search algorithm for automatic quantization.
"""
import torch
import torch.nn as nn
from distiller.quantization.range_linear import PostTrainLinearQuantizer, ClipMode, LinearQuantMode, FP16Wrapper
from distiller.summary_graph import SummaryGraph
import distiller.modules
from distiller.data_loggers import collect_quant_stats
from distiller.models import create_model
from collections import OrderedDict
import logging
from copy import deepcopy
import distiller.apputils.image_classifier as classifier
import os
import distiller.apputils as apputils
import re

__all__ = ['ptq_greedy_search']

msglogger = None

QUANTIZED_MODULES = (
    nn.Linear,
    nn.Conv2d,
    nn.Conv3d,
    distiller.modules.Concat,
    distiller.modules.EltwiseAdd,
    distiller.modules.EltwiseMult,
    distiller.modules.Matmul,
    distiller.modules.BatchMatmul
)

FP16_LAYERS = (
    nn.ReLU,
    nn.Tanh,
    nn.Sigmoid
)

PARAM_MODULES = (
    nn.Linear,
    nn.Conv2d,
    nn.Conv3d
)

UNQUANTIZED_MODULES = (
    nn.Softmax,
)

CLIP_MODES = ['NONE',
              'AVG',
              'GAUSS',
              'LAPLACE'
              ]


def module_override(**kwargs):
    override = OrderedDict()
    if kwargs.get('fp16', False):
        override['fp16'] = True
        return override
    return OrderedDict(kwargs)


def module_override_generator(module):
    if isinstance(module, FP16_LAYERS):
        yield module_override(fp16=True)
        return
    if isinstance(module, UNQUANTIZED_MODULES) or not isinstance(module, QUANTIZED_MODULES):
        yield module_override()
        return

    # Module is quantized to int8:
    for clip_mode in CLIP_MODES:
        if isinstance(module, PARAM_MODULES):
            current_module_override = module_override(clip_acts=clip_mode,
                                                      bits_weights=8,
                                                      bits_activations=8,
                                                      bits_bias=32)
        else:
            current_module_override = module_override(clip_acts=clip_mode,
                                                      bits_weights=8,
                                                      bits_activations=8)
        yield current_module_override


def ptq_greedy_search(model, dummy_input, eval_fn, calib_eval_fn=None,
                      recurrent=False, act_stats=None,
                      args=None, module_override_gen_fn=None):
    """
    Perform greedy search on Post Train Quantization configuration for the model.
    Args:
        model (nn.Module): the model to quantize
        dummy_input (torch.Tensor): a dummy input to be passed to the model
        eval_fn (function): Test/Evaluation function for the model. It must have an argument named 'model' that
          accepts the model. All other arguments should be set in advance (can be done using functools.partial), or
          they will be left with their default values.
        calib_eval_fn (function): An 'evaluation' function to use for forward passing
          through the model to collection quantization calibration statistics.
          if None provided - will use `eval_fn` as a default.
        recurrent (bool): a flag to indicate whether the model has recurrent connections.
        act_stats (OrderedDict): quant calibration activation stats.
          if None provided - will be calculated on runtime.
        args: command line arguments
        module_override_gen_fn: A function to generate module overrides.
          assumes signature `def module_override_gen_fn(module: nn.Module) -> Generator[OrderedDict, None, None]`.
    Returns:
        (quantized_model, best_overrides_dict)
    Note:
        It is assumed that `eval_fn` returns a satisfying metric of performance (e.g. accuracy)
        and the greedy search aims to maximize this metric.
    """
    best_overrides_dict = OrderedDict()
    overrides_dict = OrderedDict()
    sg = SummaryGraph(model, dummy_input)
    modules_to_quantize = sg.layers_topological_order(recurrent)
    adjacency_map = sg.adjacency_map()
    modules_dict = dict(model.named_modules())
    modules_to_quantize = [m for m in modules_to_quantize
                           if m not in args.qe_no_quant_layers]

    module_override_gen_fn = module_override_gen_fn or module_override_generator

    calib_eval_fn = calib_eval_fn or eval_fn
    if not act_stats:
        msglogger.info('Collecting stats for model...')
        model_temp = distiller.utils.make_non_parallel_copy(model)
        act_stats = collect_quant_stats(model_temp, calib_eval_fn)
        del model_temp
        if args:
            act_stats_path = '%s_act_stats.yaml' % args.arch
            msglogger.info('Done. Saving act stats into %s' % act_stats_path)
            distiller.yaml_ordered_save(act_stats_path, act_stats)
    base_score = eval_fn(model)
    msglogger.info("Base score: %.3f" % base_score)

    def recalibrate_stats(module_name, act_stats):
        """
        Re-collects quant-calibration stats for successor modules of the current module.
        """
        modules_to_recalibrate = {op.name for op in adjacency_map[module_name].successors} & set(act_stats)
        if not modules_to_recalibrate:
            # either there aren't any successors or
            # the successors aren't in the stats file - skip
            return act_stats
        q = PostTrainLinearQuantizer(distiller.utils.make_non_parallel_copy(model),
                                     bits_activations=None,
                                     bits_parameters=None,
                                     bits_accum=32,
                                     mode=LinearQuantMode.ASYMMETRIC_SIGNED,
                                     clip_acts=ClipMode.NONE,
                                     overrides=deepcopy(best_overrides_dict),
                                     model_activation_stats=deepcopy(act_stats))
        q.prepare_model(dummy_input)
        # recalibrate on the current best quantized version of the model.
        recalib_act_stats = collect_quant_stats(q.model, calib_eval_fn, modules_to_collect=modules_to_recalibrate)
        act_stats.update(recalib_act_stats)
        return act_stats

    for module_name in modules_to_quantize:
        msglogger.info('Searching optimal quantization in \'%s\':' % module_name)
        module = modules_dict[module_name]
        overrides_dict = deepcopy(best_overrides_dict)
        best_performance = float("-inf")
        normalized_module_name = module_name
        if isinstance(model, nn.DataParallel):
            normalized_module_name = re.sub(r'module\.', '', normalized_module_name)
        for current_module_override in module_override_gen_fn(module):
            overrides_dict[normalized_module_name] = current_module_override
            temp_act_stats = deepcopy(act_stats)
            quantizer = PostTrainLinearQuantizer(deepcopy(model),
                                                 bits_activations=None,
                                                 bits_parameters=None,
                                                 bits_accum=32,
                                                 mode=LinearQuantMode.ASYMMETRIC_SIGNED,
                                                 clip_acts=ClipMode.NONE,
                                                 overrides=deepcopy(overrides_dict),
                                                 model_activation_stats=deepcopy(temp_act_stats))
            quantizer.prepare_model(dummy_input)

            current_perf = eval_fn(quantizer.model)
            if isinstance(module, QUANTIZED_MODULES):
                clip_mode = current_module_override['clip_acts']
                msglogger.info('\t%s\t score = %.3f\tLayer overrides: %s' %
                      (clip_mode, current_perf, current_module_override))
            else:
                msglogger.info('\t Module is not quantized to int8. Not clipping activations.')
                msglogger.info('\t score = %.3f\tLayer overrides: %s' %
                      (current_perf, current_module_override))
            if current_perf > best_performance:
                best_overrides_dict[normalized_module_name] = current_module_override
                best_performance = current_perf

        # end of search - we update the calibration of the next layers:
        recalibrate_stats(module_name, act_stats)

    quantizer = PostTrainLinearQuantizer(model, mode=LinearQuantMode.ASYMMETRIC_SIGNED,
                                         clip_acts=ClipMode.NONE, overrides=deepcopy(best_overrides_dict),
                                         model_activation_stats=act_stats)
    quantizer.prepare_model(dummy_input)
    msglogger.info('best_overrides_dict: %s' % best_overrides_dict)
    msglogger.info('Best score ', eval_fn(quantizer.model))
    return model, best_overrides_dict


def config_verbose(verbose):
    if verbose:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.INFO
        logging.getLogger().setLevel(logging.WARNING)
    for module in ["distiller.apputils.image_classifier", ]:
        logging.getLogger(module).setLevel(loglevel)


if __name__ == "__main__":
    parser = classifier.init_classifier_compression_arg_parser()
    parser.add_argument('--qe-no-quant-layers', '--qenql', type=str, nargs='+', metavar='LAYER_NAME', default=[],
                       help='List of layer names for which to skip quantization.')
    parser.add_argument('--qe-calib-portion', type=float, default=1.0,
                        help='The portion of the dataset to use for calibration stats collection.')
    parser.add_argument('--qe-calib-batchsize', type=int, default=256,
                        help='The portion of the dataset to use for calibration stats collection.')
    args = parser.parse_args()
    cc = classifier.ClassifierCompressor(args, script_dir=os.path.dirname(__file__))
    eval_data_loader = classifier.load_data(args, load_train=False, load_val=False)

    # quant calibration dataloader:
    args.effective_test_size = args.qe_calib_portion
    args.batch_size = args.qe_calib_batchsize
    calib_data_loader = classifier.load_data(args, load_train=False, load_val=False)
    # logging
    logging.getLogger().setLevel(logging.WARNING)
    msglogger = logging.getLogger(__name__)
    msglogger.setLevel(logging.INFO)

    def test_fn(model):
        top1, top5, losses = classifier.test(eval_data_loader, model, cc.criterion, [cc.tflogger, cc.pylogger], None,
                                             args)
        return top1

    def calib_eval_fn(model):
        classifier.test(calib_data_loader, model, cc.criterion, [], None,
                        args)

    model = create_model(args.pretrained, args.dataset, args.arch,
                         parallel=not args.load_serialized, device_ids=args.gpus)
    args.device = next(model.parameters()).device
    if args.load_model_path:
        msglogger.info("Loading checkpoint from %s" % args.load_model_path)
        model = apputils.load_lean_checkpoint(model, args.load_model_path,
                                              model_device=args.device)
    dummy_input = torch.rand(*model.input_shape, device=args.device)
    if args.qe_stats_file:
        msglogger.info("Loading stats from %s" % args.qe_stats_file)
        with open(args.qe_stats_file, 'r') as f:
            act_stats = distiller.yaml_ordered_load(f)
    else:
        act_stats = None
    m, overrides = ptq_greedy_search(model, dummy_input, test_fn,
                                     calib_eval_fn=calib_eval_fn, args=args,
                                     act_stats=act_stats)
    distiller.yaml_ordered_save('%s.ptq_greedy_search.yaml' % args.arch, overrides)