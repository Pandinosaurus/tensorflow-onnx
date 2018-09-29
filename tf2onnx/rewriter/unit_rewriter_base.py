# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter.rnn_unit_base - lstm support
"""

from __future__ import division
from __future__ import print_function

import collections
import logging
import numpy as np
import tf2onnx
from onnx import helper, defs, numpy_helper, checker, onnx_pb
from onnx import AttributeProto, TensorProto, GraphProto
from tf2onnx import utils
from tf2onnx.graph import Node, Graph
from tf2onnx.graph_matcher import *
from tf2onnx.rewriter.rnn_utils import *

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tf2onnx.rewriter.rnn_unit_writer_base")

# dynamic_rnn or bidirectional_dynamic_rnn related logic will be mapped to this base calls.
class UnitRewriterBase:
    def __init__(self, g):
        self.g = g
        self.all_nodes = self.g.get_nodes()
        # used to track nodes in rnn_scope_name to keep for each single match
        self.must_keep_nodes = []

    def print_step(self, level_2, level_1 = "find_dynamic_run_lstm"):
        log.info(level_1 + " >> " + level_2)

    def ct_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        pass

    def ht_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        pass

    # when state is not tuple, ct and ht may share same switch.
    def ct_ht_shared_switch_check(self, enter_target_node_input_id, identity_consumers, match):
        pass

    def get_rnn_scope_name(self, match):
        pass

    def get_weight_and_bias(self, match):
        pass

    def process_weights_and_bias(self, rnn_weights):
        pass

    def get_rnn_input_backlist(self, rnn_weights, rnn_inits):
        if rnn_inits.share_init_node:
            ch_node = self.g.get_node_by_name(rnn_inits.share_init_input_id)
            c_h_nodes = [ch_node]
            self.must_keep_nodes.append(ch_node)
        else:
            c_h_nodes = [self.g.get_node_by_name(rnn_inits.c_init_input_id), 
                         self.g.get_node_by_name(rnn_inits.h_init_input_id)]

        # weight/bias inputs, and c/h initializers are dynamic_rnn/lstmcell's parameters.
        # we will use them to filter out the dynamic_rnn's input tensor. 
        blacklist_inputs = [rnn_weights.kernel.node, rnn_weights.bias.node, rnn_weights.forget_bias.node ]
        blacklist_inputs.extend(c_h_nodes)

        return blacklist_inputs

    def run(self, unit_type):
        # allow_reorder must be true. because LSTMCell and BasicLSTMCell's call function
        # are defining the calculation with different orders. Then we can share the same 
        # pattern.
        cell_pattern = get_pattern(unit_type)
        matcher = GraphMatcher(cell_pattern, allow_reorder=True)
        match_results = list(matcher.match_ops(self.g.get_nodes()))
        for match in match_results:
            self.run_single_match(match)
        self.print_step("finish handling")
        self.g.update_proto()

        return self.g.get_nodes()

    def run_single_match(self, match):
        log.info("=========================")
        self.print_step("start handling a new potential LSTM Cell")
        self.all_nodes = self.g.get_nodes()
        self.must_keep_nodes = []

        rnn_scope_name = self.get_rnn_scope_name(match)
        if not rnn_scope_name:
            log.error("unable to find rnn scope name, skip")
            return REWRITER_RESULT.SKIP
        else:
            log.info("rnn scope name is " + rnn_scope_name)

        self.print_step("get_weight_and_bias starts")
        rnn_weights = self.get_weight_and_bias(match)
        if not rnn_weights:
            log.error("basic LSTM Cell weights check failed, skip")
            return REWRITER_RESULT.SKIP

        rnn_inits = self.get_ct_ht_initializers(match, rnn_scope_name)
        if not rnn_inits:
            log.error("basic LSTM Cell ct/ht initializer check failed, skip")
            return REWRITER_RESULT.SKIP

        input_filter = self.get_rnn_input_backlist(rnn_weights, rnn_inits)

        rnn_props = self.find_input_and_connectors(rnn_scope_name, input_filter)
        if not rnn_props.is_valid():
            log.error("RNN properties are not valid, skip")
            return REWRITER_RESULT.SKIP

        if not self.process_lstm_input_x(rnn_props, rnn_scope_name):
            log.error("RNN input x not found, skip")
            return REWRITER_RESULT.SKIP

        self.print_step("process the weights/bias/ft_bias, to fit onnx weights/bias requirements")
        W, R, B, input_size, hidden_size = self.process_weights_and_bias(rnn_weights)
        rnn_props.input_size = input_size
        rnn_props.hidden_size = hidden_size

        lstm_name = utils.make_name("LSTM")
        new_rnn_scope_name = lstm_name + "/"

        # create node
        w_name = utils.make_name(new_rnn_scope_name + "W")
        w_node = self.g.make_const(w_name, W, skip_conversion = True)

        r_name = utils.make_name(new_rnn_scope_name + "R")
        r_node = self.g.make_const(r_name, R, skip_conversion = True)

        b_name = utils.make_name(new_rnn_scope_name + "B")
        b_node = self.g.make_const(b_name, B, skip_conversion = True)


        init_h_id = None
        init_c_id = None
        if rnn_inits.share_init_node:
            init_h_id, init_c_id = self.process_non_tuple_ch_init_nodes(rnn_inits.share_init_input_id, hidden_size)
        else:
            init_h_id, init_c_id = self.process_tuple_ch_init_nodes(rnn_inits.c_init_input_id, rnn_inits.h_init_input_id, hidden_size)
        assert init_h_id and init_c_id

        len_node = self.create_seq_len_node(rnn_props)

        self.print_step("start to build new LSTM node")

        # specify if the RNN is forward, reverse, or bidirectional. Must be one of forward (default), reverse, or bidirectional.
        # Here we won't mark bidirectional, we will have another rewriter running after this one, which will based 
        # on patterns to combine a forward LSTM and a backward LSTM into a bidirectional one.
        direction = "forward"
        if rnn_props.is_backward:
            direction = "reverse"
        # todo: input_forget
        attr = { "direction": direction, "hidden_size": hidden_size}
        lstm_input_nodes = [rnn_props.x_node, w_node, r_node, b_node, len_node]
        lstm_inputs = list(map(lambda n: n.output[0], lstm_input_nodes))
        lstm_inputs.extend([init_h_id, init_c_id])

        lstm_outputs = [lstm_name + ":" + str(i) for i in np.arange(3)]
        lstm_node = Node(helper.make_node("LSTM", lstm_inputs , lstm_outputs, name=lstm_name, **attr), self.g, skip_conversion = True)

        self.all_nodes.extend([lstm_node])

        self.print_step("start to handle output connectors")
        self.process_output_connectors(match, lstm_node, rnn_props, rnn_scope_name)

        self.print_step("remove all nodes within original rnn scope except some nodes still useful")
        new_nodes = []
        for n in self.all_nodes:
            if n in self.must_keep_nodes:
                new_nodes.append(n)
                continue
            else:
                if n.name.startswith(rnn_scope_name):
                    pass
                else:
                    new_nodes.append(n)

        self.g.set_nodes(new_nodes)


    def find_input_and_connectors(self, rnn_scope_name, input_blacklist = None):
        rnn_props = RnnProperties()
        rnn_input_nodes = []
        connector_nodes = []
        for n in self.g.get_nodes():
            if n.name.startswith(rnn_scope_name):
                # find input node that are not within rnn scope
                for input_id, input_node in zip(n.input, n.inputs):
                    if not input_node.name.startswith(rnn_scope_name):
                        if input_node not in input_blacklist:
                            rnn_input_nodes.append([input_node, input_id])

                # find output consumers that are not within runn scope
                for output_name in n.output:
                    output_nodes = self.g.find_output_consumers(output_name)
                    for out_node in output_nodes:
                        if not out_node.name.startswith(rnn_scope_name):
                            connector_nodes.append(out_node)

        if len(rnn_input_nodes) != 1:
            log.error("found 2 inputs for the dynamic_run, unexpected. They are ")
            log.error(rnn_input_nodes)
            return rnn_props

        input_node_candidate = rnn_input_nodes[0][0]
        input_id_candidate = rnn_input_nodes[0][1]

        # in TF bidirectional_rnn, backforward, will first reverse the inputs, then dynamic_run, then reverse 
        # output back. And the 2 reverses operators are not within the deeper dymanic_rnn scope. 
        # So, in this case, we might get a ReverseV2 op in rnn_input_nodes. 
        if input_node_candidate.type in ["ReverseV2"]:
            log.info("found reverse pattern")
            rnn_props.is_backward = True

            # ReverseV2 has 2 inputs, the second is axis.
            rnn_props.input_node = input_node_candidate.inputs[0]
            rnn_props.input_id = input_node_candidate.input[0]
            rnn_props.connectors = connector_nodes
            return rnn_props
        else:
            # we should not limit the rnn_input_nodes' type be PlaceHolder or Const, 
            # because there might some Reshape/etc. ops after the Placeholder
            rnn_props.input_node = input_node_candidate
            rnn_props.input_id = input_id_candidate
            rnn_props.connectors = connector_nodes
            return rnn_props

    def get_ct_ht_initializers(self, match, rnn_scope_name):
        loop_cond_op = None
        for n in self.g.get_nodes():
            if n.type == 'LoopCond' and n.name.startswith(rnn_scope_name):
                if not loop_cond_op:
                    loop_cond_op = n
                else:
                    raise ValueError("only a LoopCond is expected to find in a dynamic run")

        if loop_cond_op == None:
            log.error("No LoopCond op is found, skip")
            return None

        # be noted: dynamic_rnn's initial_state can be constant or not.
        h_initializer = None
        c_initializer = None
        shared_ch_initializer_input_id = None # only for non-tuple c_h initializer
        switch_nodes = self.g.find_output_consumers(loop_cond_op.output[0])
        for n in switch_nodes:
            if n.type != 'Switch':
                raise ValueError("LoopCond's output node should be followed with a Switch node")
            enter_target_input_id = self.check_switch_by_usage_pattern(n, match, self.ct_switch_check)
            if enter_target_input_id:
                c_initializer = enter_target_input_id
                continue

            enter_target_input_id = self.check_switch_by_usage_pattern(n, match, self.ht_switch_check)
            if enter_target_input_id:
                h_initializer = enter_target_input_id
                continue

            enter_target_input_id = self.check_switch_by_usage_pattern(n, match, self.ct_ht_shared_switch_check)
            if enter_target_input_id:
                shared_ch_initializer_input_id = enter_target_input_id
                continue

        # when shared_ch_initializer_input_id is not None, c_initializer and h_initializer
        # should be None, and vice versa
        if shared_ch_initializer_input_id:
            assert not c_initializer and not h_initializer
        else:
            assert not shared_ch_initializer_input_id

        return RnnInitializers(c_initializer, h_initializer, shared_ch_initializer_input_id)

    def check_switch_by_usage_pattern(self, switch_node, match, check_func):
        if switch_node.type != 'Switch':
            return None

        # the first input is data
        merge_node = switch_node.inputs[0]
        if merge_node.type != "Merge":
            return None

        target_node_input_id = None
        for merge_input in merge_node.inputs:
            if merge_input.type == 'Enter':
                target_node_input_id = merge_input.input[0]
                log.debug("a Switch >> Merge >> Enter is found called " + merge_input.inputs[0].name)
                break
            else:
                log.debug("skip the non-Enter input node of the merge_node")
                continue

        # check whether it is c_initialize or h_initialize
        if target_node_input_id:
            switch_consumers = self.g.find_output_consumers(switch_node.output[1])
            assert len(switch_consumers) == 1
            if switch_consumers[0].type == "Identity":
                identity_consumers = self.g.find_output_consumers(switch_consumers[0].output[0])
                return check_func(target_node_input_id, identity_consumers, match)
            else:
                log.error("not expected, skip ")
        else:
            log.warning("is_switch_used_by found no merge>>Enter node")

        return None

    def process_lstm_input_x(self, rnn_props, rnn_scope_name):
        self.print_step("look for possible transpose following RNN input node")
        # todo: peepholdes P is not considered now
        input_consumers = self.g.find_output_consumers(rnn_props.input_id)
        cur_rnn_consumers = []
        for consumer in input_consumers:
            if not rnn_props.is_backward:
                if consumer.name.startswith(rnn_scope_name):
                    cur_rnn_consumers.append(consumer)
            else:
                # reversev2 might have a different name scope, so we check this way.
                if consumer.type == "ReverseV2":
                    cur_rnn_consumers.append(consumer)

        if len(cur_rnn_consumers) != 1:
            log.error("RNN input node has " + str(len(cur_rnn_consumers)) + " consumers in current rnn scope " + rnn_scope_name + ", skip")
            return None

        possible_transpose_after_input = cur_rnn_consumers[0]
        if rnn_props.is_backward:
            assert possible_transpose_after_input.type == "ReverseV2"
            self.must_keep_nodes.append(possible_transpose_after_input)
            reverse_outputs = self.g.find_output_consumers(possible_transpose_after_input.output[0])
            assert len(reverse_outputs) == 1 # bidirectional_dynamic_rnn logic will promise this
            possible_transpose_after_input = reverse_outputs[0]


        self.print_step("convert the transpose to onnx node if there is one found.")
        # check whether time_major is enabled or not
        # in TF, if time_major is not enabled, input format is [batch, time, ...]
        # but, during TF handling, at the beginning, the data will be transposed to [time, batch, ...]
        # after processing, the format is changed back before returning result.
        # So here, we judge the time_major by checking the transpose operator existence.
        converted_transpose = self.convert_timemajor_transpose(possible_transpose_after_input)
        if converted_transpose:
            log.debug("detect batch-major inputs")
            rnn_props.time_major = False
            rnn_props.x_node = converted_transpose
            self.all_nodes.extend([converted_transpose])
        else:
            log.debug("detect timer-major inputs")
            rnn_props.time_major = True
            rnn_props.x_node = rnn_props.input_node

        return rnn_props

    def convert_timemajor_transpose(self, node):
        if not check_is_timemajor_transpose(node):
            log.debug("not found timemajor transpose")
            return

        new_trans_name = utils.make_name("Transpose")
        attr = {"perm": np.array([1, 0, 2], dtype=np.int64) }
        new_trans = Node(helper.make_node("Transpose", [node.input[0]], [new_trans_name + ":0"], name=new_trans_name, **attr), self.g, skip_conversion = True)
        self.g.copy_shape(node.output[0], new_trans.output[0])
        self.g.replace_all_inputs(self.g.get_nodes(), node.output[0], new_trans.output[0])
        return new_trans

    def process_non_tuple_ch_init_nodes(self, input_id, hidden_size):
        op_name = utils.make_name("Slice")
        attr = { "axes": [1], "starts": [0], "ends": [hidden_size] }
        slice_node1 = Node(helper.make_node("Slice", [input_id], [op_name + ":0"], name=op_name, **attr), self.g, skip_conversion = True)
        op_name = utils.make_name("Unsqueeze")
        squeeze_node_1 = Node(helper.make_node("Unsqueeze", [slice_node1.output[0]], [op_name + ":0"], name=op_name, axes=[0]), self.g, skip_conversion = True)

        op_name = utils.make_name("Slice")
        attr = { "axes": [1], "starts": [hidden_size], "ends": [hidden_size*2] }
        slice_node2 = Node(helper.make_node("Slice", [input_id], [op_name + ":0"], name=op_name, **attr), self.g, skip_conversion = True)
        op_name = utils.make_name("Unsqueeze")
        squeeze_node_2 = Node(helper.make_node("Unsqueeze", [slice_node2.output[0]], [op_name + ":0"], name=op_name, axes=[0]), self.g, skip_conversion = True)

        self.all_nodes.extend([slice_node1, slice_node2, squeeze_node_1, squeeze_node_2])
        return squeeze_node_1.output[0], squeeze_node_2.output[0]

    def process_tuple_ch_init_nodes(self, c_init_input_id, h_init_input_id, hidden_size):
        h_node_output = self.connect_initializer_node(h_init_input_id, hidden_size)
        c_node_output = self.connect_initializer_node(c_init_input_id, hidden_size)
        return h_node_output, c_node_output

    def connect_initializer_node(self, initializer_input_id, hidden_size):
        node = self.g.get_node_by_name(initializer_input_id)
        if node.is_const():
            val = node.get_tensor_value()
            initial_name = utils.make_name("Const")
            new_val = np.expand_dims(val, axis=0)
            const_node = self.g.make_const(initial_name, new_val)
            return const_node.output[0]
        else:
            op_name = utils.make_name("Unsqueeze")
            squeeze_node = Node(helper.make_node("Unsqueeze", [initializer_input_id], [op_name + ":0"], name=op_name, axes=[0]), self.g, skip_conversion = True)
            self.g.replace_all_inputs(self.g.get_nodes(), initializer_input_id, squeeze_node.output[0])
            self.all_nodes.append(squeeze_node)
            return squeeze_node.output[0]

    def create_seq_len_node(self, rnn_props):
        # check whether input_node has valid shape
        if rnn_props.input_node.shape:
            self.print_step("input node has shape, so parse out batch_size, time_step, input_size. and create seq_len constant nodes")
            if rnn_props.time_major and rnn_props.input_node.shape[1] != utils.ONNX_UNKNOWN_DIMENSION:
                time_step = rnn_props.input_node.shape[0]
                batch_size = rnn_props.input_node.shape[1]
                input_size_2 = rnn_props.input_node.shape[2]
            elif rnn_props.input_node.shape[0] != utils.ONNX_UNKNOWN_DIMENSION:
                batch_size = rnn_props.input_node.shape[0]
                time_step = rnn_props.input_node.shape[1]
                input_size_2 = rnn_props.input_node.shape[2]

            assert rnn_props.input_size == input_size_2
            # todo: what if batch_size = -1
            sequence_lens = np.array([time_step for l in np.arange(batch_size)], dtype=np.int32)
            len_name = utils.make_name("Const")
            len_node = self.g.make_const(len_name, sequence_lens, skip_conversion = True)
            self.g.set_shape(len_node.output[0], sequence_lens.shape)
        else:
            self.print_step("prepare input nodes for new lstm node")
            shape_node_name = utils.make_name("Shape")
            shape_node = Node(helper.make_node("Shape", [rnn_props.input_id] , [ shape_node_name+ ":0"], name=shape_node_name), self.g, skip_conversion = True)
            # LSTMCell only allow inputs of [batch, input_size], so we assume dynamic_rnn has 3 dims.
            # Slice cannot support Int64 in OPSET 7, so we cast here.
            cast_shape_name = utils.make_name("Cast")
            attr = { "to" : onnx_pb.TensorProto.FLOAT }
            cast_shape_node = Node(helper.make_node('Cast', [shape_node.output[0]], [cast_shape_name + ":0"], name = cast_shape_name, **attr), self.g, skip_conversion = True)
            self.g.copy_shape(shape_node.output[0], cast_shape_node.output[0])

            batchsize_name = utils.make_name("Slice")
            batch_attr = { "axes" : [0], "starts": [0], "ends": [1] }
            batchsize_node = Node(helper.make_node('Slice', [cast_shape_node.output[0]], [batchsize_name + ":0"], name = batchsize_name, **batch_attr), self.g, skip_conversion = True)

            repeat_name = utils.make_name("Cast")
            attr = { "to" : onnx_pb.TensorProto.INT64}
            repeat_node = Node(helper.make_node('Cast', [batchsize_node.output[0]], [repeat_name + ":0"], name = repeat_name, **attr), self.g, skip_conversion = True)


            timestep_name = utils.make_name("Slice")
            ts_attr = { "axes" : [0], "starts": [1], "ends": [2] }
            timestep_node = Node(helper.make_node('Slice', [cast_shape_node.output[0]], [timestep_name + ":0"], name = timestep_name, **ts_attr), self.g, skip_conversion = True)

            tile_name = utils.make_name("Tile")
            tile_node = Node(helper.make_node('Tile', [timestep_node.output[0], repeat_node.output[0]], [tile_name + ":0"], name = tile_name), self.g, skip_conversion = True)

            cast_back_name = utils.make_name("Cast")
            attr = { "to" : onnx_pb.TensorProto.INT32} # LSTM sequence_lens needs to be int32
            cast_back_node = Node(helper.make_node('Cast', [tile_node.output[0]], [cast_back_name + ":0"], name = cast_back_name, **attr), self.g, skip_conversion = True)

            len_node = cast_back_node
            self.all_nodes.extend([shape_node, cast_shape_node, timestep_node, batchsize_node, repeat_node, tile_node, len_node])

        return len_node


    def process_output_connectors(self, match, lstm_node, rnn_props, rnn_scope_name):
        # There are 2 kinds of output nodes for dynamic_rnn
        # 1. output node, which would either ends with a Transpose (when time_major is False), or ends with TensorArrayGatherV3
        # 2. cell_state node, 
        #    2.1 if state_is_tuple is true:
        #        2.1.1 which would ends with a Pack<C, H> operator when cell_state is used.
        #        2.1.2 which would ends with "Exit" for c and h respectively, when cell_state.c/h is used.
        #    2.2 which would ends with "Exit" if state_is_tupe is false
        connector_nodes = set(rnn_props.connectors)
        for n in connector_nodes:
            log.debug("processiong connector node called "+ n.name)
            # todo: change to another way
            if n.need_skip():
                log.debug("newly created nodes, won't consider as RNN outputs.")
                continue

            if rnn_props.is_backward:
                # Y handler
                if n.type == "ReverseV2" and n.inputs[0].type in ["Transpose", "TensorArrayGatherV3"] and n.inputs[0].name.startswith(rnn_scope_name):
                    input_n = n.inputs[0]
                    n.input[0] = lstm_node.output[0]
                    new_nodes = self.create_transform_nodes_after_lstm(input_n, n, rnn_props.time_major)
                    self.g.replace_all_inputs(self.all_nodes, n.output[0], new_nodes[-1].output[0])
                    self.all_nodes.extend(new_nodes)
                    # Y_h/Y_c handler 
                elif self.check_is_consumer_of_tupled_ch(n, match):
                    # For reverse, unlike output node (who is followed by a reversev2), 
                    # the Pack node generating cell_state don't have reversev2 followed.
                    self.connect_rnn_with_tupled_ch_consumer_nodes(lstm_node, n)
                # todo: non-tupled check
            else:
                # tupled Y_c/Y_h handling, use tuple directly
                if self.check_is_consumer_of_tupled_ch(n, match):
                    self.connect_rnn_with_tupled_ch_consumer_nodes(lstm_node, n)
                else:
                    to_replace = {}
                    for input_id, input_n in zip(n.input, n.inputs):
                        if not input_n:
                            log.debug("node " + input_id + " is none, skip")
                            continue
                        if not input_n.name.startswith(rnn_scope_name):
                            log.debug("skip " + input_n.name)
                            continue
                        else:
                            # Y handler
                            if self.check_is_rnn_outputs_node(input_n, rnn_props.time_major):
                                log.debug("this is the rnn output node's consumer")
                                new_nodes = self.create_transform_nodes_after_lstm(self.g, lstm_node, rnn_props.time_major)
                                to_replace[input_id] = new_nodes[-1].output[0]
                                self.all_nodes.extend(new_nodes)
                            else:
                                error_code = self.check_is_consumer_of_exit_after_ch(match, input_n)
                                if error_code == 1: # tupled Y_c/Y_h handling, use tuple.c
                                    self.connect_rnn_with_one_of_tupled_ch_consumer_nodes(lstm_node.output[2], input_id)
                                elif error_code == 2: # tupled Y_c/Y_h handling, use tuple.h
                                    self.connect_rnn_with_one_of_tupled_ch_consumer_nodes(lstm_node.output[1], input_id)
                                elif error_code == 3: # non-tupled Y_c/Y_h handling. (shared same Exit)
                                    self.connect_rnn_with_non_tupled_ch_consumer_nodes(lstm_node, n, input_id)
                                else:
                                    raise ValueError("not match rnn output node, skip " + input_n.name)
                    for input_id in to_replace:
                        self.g.replace_all_inputs(self.all_nodes, input_id, to_replace[input_id])

    # c: memory (in TF, it was called hidden state)
    # h: hidden state (in TF, it was called output)
    def check_is_consumer_of_tupled_ch(self, n, match):
        # This Pack is generated when dynamic_rnn return cell_state as a tuple, e.g <c, h>.
        # Pack's name is not in the rnn scope.
        if not (n.type == "Pack" and len(n.inputs) == 2):
            log.debug("check_is_ch_output_node Pack check fail")
            return False

        exit_1 = n.inputs[0]
        exit_2 = n.inputs[1]
        if not (exit_1 and exit_1.type == "Exit" and exit_2 and exit_2.type == "Exit"):
            log.debug("check_is_ch_output_node Exit check fail")
            return False

        switch_1 = exit_1.inputs[0]
        switch_2 = exit_2.inputs[0]
        if not (switch_1.type == "Switch" and switch_2.type == "Switch"):
            log.debug("check_is_ch_output_node Switch check fail")
            return False

        ct_enter_target_node = None
        ht_enter_target_node = None
        for s in [switch_1, switch_2]:
            enter_target_input_id = self.check_switch_by_usage_pattern(s, match, self.ct_switch_check)
            if enter_target_input_id:
                ct_enter_target_node = enter_target_input_id
                continue

            enter_target_input_id = self.check_switch_by_usage_pattern(s, match, self.ht_switch_check)
            if enter_target_input_id:
                ht_enter_target_node = enter_target_input_id
                continue

        if ct_enter_target_node and ht_enter_target_node:
            return True

        log.debug("fail to found ct and ht node based on pattern")
        return False

    def check_is_consumer_of_exit_after_ch(self, match, connector_in_rnnscope):
        if not (connector_in_rnnscope and connector_in_rnnscope.type == "Exit"):
            log.debug("check_is_consumer_of_exit_after_ch Exit check fail")
            return False

        switch = connector_in_rnnscope.inputs[0]
        if not (switch.type == "Switch"):
            log.debug("check_is_consumer_of_exit_after_ch Switch check fail")
            return False

        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ct_switch_check)
        if enter_target_input_id:
            return 1
        
        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ht_switch_check)
        if enter_target_input_id:
            return 2

        enter_target_input_id = self.check_switch_by_usage_pattern(switch, match, self.ct_ht_shared_switch_check)
        if enter_target_input_id:
            return 3

        log.debug("check_is_consumer_of_exit_after_ch fail to found ct and ht node based on pattern")
        return False

    def check_is_rnn_outputs_node(self, connector_in_rnnscope, time_major):
        node_to_check = connector_in_rnnscope
        if not time_major:
            # in batch_major mode, rnn outputs will ends with a Tranpose. So
            # here we check the Transpose. 

            # Be noted, in TF, transpose has 2 inputs.
            if not (len(connector_in_rnnscope.inputs) == 2 and check_is_timemajor_transpose(connector_in_rnnscope)):
                log.debug("check_is_rnn_outputs_node error, in batch_major mode, Transpose should be found but actually not.")
                return False
            # the first input is data
            node_to_check = connector_in_rnnscope.inputs[0]

        if node_to_check.type in ["TensorArrayGatherV3"]:
            log.debug("Find output node " + connector_in_rnnscope.name)
            return True

    def create_transform_nodes_after_lstm(self, input_n, parent_node, time_major):
        # here we gave up existing transpose, instead, add some ops based on lstm node's result (indirect or directly)
        # just make sure the final output is [batch, time, hidden]

        # insert Squeeze in axes 1
        op_name = utils.make_name("Squeeze")
        # lstm's 1st output shape is [time, num_directions, batch, hidden]
        squeeze_node = Node(helper.make_node("Squeeze", [parent_node.output[0]], [op_name+":0"], name=op_name, axes=[1]), self.g, skip_conversion = True)

        if not time_major:
            # transpose to [batch, time, hidden], since node n orignally use this
            new_trans_name = utils.make_name("Transpose")
            attr={ "perm": np.array([1, 0, 2], dtype=np.int64) }
            new_trans = Node(helper.make_node("Transpose", [squeeze_node.output[0]], [new_trans_name + ":0"], name=new_trans_name, **attr), self.g, skip_conversion = True)

            return [squeeze_node, new_trans]
        else:
            assert input_n.type == "TensorArrayGatherV3"
            return [squeeze_node]

    def connect_rnn_with_tupled_ch_consumer_nodes(self, lstm_node, connector_node_outside_rnn_scope):
        n = connector_node_outside_rnn_scope
        assert len(n.input) == 2
        c_slice_name = utils.make_name("Slice")
        attr = {"axes": [0], "starts": [0], "ends": [1]}
        c_slice_node = Node(helper.make_node("Slice", [lstm_node.output[2]], [c_slice_name+":0"], name=c_slice_name, **attr), self.g, skip_conversion = True)

        h_slice_name = utils.make_name("Slice")
        attr = {"axes": [0], "starts": [0], "ends": [1]}
        h_slice_node = Node(helper.make_node("Slice", [lstm_node.output[1]], [h_slice_name+":0"], name=h_slice_name, **attr), self.g, skip_conversion = True)

        n.input[0] = c_slice_node.output[0] # new c
        n.input[1] = h_slice_node.output[0] # new h

        # For all Pack's consumers, they originaly expect data [tuple_size, batch_size, hidden_size],
        # tuple_size inidicate c or h
        # BUT now, we have [tuple size, num_directions, batch_size, hidden_size]
        # since this branch handles forward only, num_directions = 1
        op_name = utils.make_name("Squeeze")
        squeeze_node = Node(helper.make_node("Squeeze", [n.output[0]], [op_name + ":0"], name=op_name, axes=[1]), self.g, skip_conversion = True)
        self.g.replace_all_inputs(self.g.get_nodes(), n.output[0], squeeze_node.output[0])

        self.all_nodes.extend([c_slice_node, h_slice_node, squeeze_node])

    def connect_rnn_with_one_of_tupled_ch_consumer_nodes(self, lstm_output_id, input_id):
        # For original consumers, they originaly expect data [batch_size, hidden_size],
        # BUT now, we have [num_directions, batch_size, hidden_size]
        # since this branch handles forward only, num_directions = 1
        op_name = utils.make_name("Squeeze")
        squeeze_node = Node(helper.make_node("Squeeze", [lstm_output_id], [op_name + ":0"], name=op_name, axes=[0]), self.g, skip_conversion = True)
        self.g.replace_all_inputs(self.all_nodes, input_id, squeeze_node.output[0])

        self.all_nodes.extend([squeeze_node])

    def connect_rnn_with_non_tupled_ch_consumer_nodes(self, lstm_node, connector_node_outside_rnn_scope, input_id):
        n = connector_node_outside_rnn_scope
        c_slice_name = utils.make_name("Slice")
        attr = {"axes": [0], "starts": [0], "ends": [1]}
        c_slice_node = Node(helper.make_node("Slice", [lstm_node.output[2]], [c_slice_name+":0"], name=c_slice_name, **attr), self.g, skip_conversion = True)

        h_slice_name = utils.make_name("Slice")
        attr = {"axes": [0], "starts": [0], "ends": [1]}
        h_slice_node = Node(helper.make_node("Slice", [lstm_node.output[1]], [h_slice_name+":0"], name=h_slice_name, **attr), self.g, skip_conversion = True)

        op_name = utils.make_name("Concat")
        attr = {"axis": 2 }
        concat = Node(helper.make_node("Concat", [c_slice_node.output[0], h_slice_node.output[0] ], [op_name + ":0"], name=op_name, **attr), self.g, skip_conversion = True)

        # For all non-tuple-ch's consumers, they originaly expect data [batch_size, hidden_size*2],
        # BUT now, we have [num_directions, batch_size, hidden_size]
        # since this branch handles forward only, num_directions = 1
        op_name = utils.make_name("Squeeze")
        squeeze_node = Node(helper.make_node("Squeeze", [concat.output[0]], [op_name + ":0"], name=op_name, axes=[0]), self.g, skip_conversion = True)
        self.g.replace_input(n, input_id, squeeze_node.output[0])

        self.all_nodes.extend([c_slice_node, h_slice_node, concat, squeeze_node])