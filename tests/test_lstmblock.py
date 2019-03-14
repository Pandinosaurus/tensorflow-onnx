# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""Unit Tests for lstm block cell."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import numpy as np
import tensorflow as tf

from tensorflow.contrib import rnn
from backend_test_base import Tf2OnnxBackendTestBase
from common import unittest_main, check_tf_min_version, check_opset_min_version


# pylint: disable=missing-docstring,invalid-name,unused-argument,using-constant-test


class LSTMBlockTests(Tf2OnnxBackendTestBase):
    @check_opset_min_version(8, "Scan")
    def test_single_dynamic_lstm(self):
        units = 5
        batch_size = 6
        x_val = np.array([[1., 1.], [2., 2.], [3., 3.], [4., 4.]], dtype=np.float32)
        x_val = np.stack([x_val] * batch_size)

        x = tf.placeholder(tf.float32, x_val.shape, name="input_1")

        # no scope
        cell = rnn.LSTMBlockCell(units, use_peephole=False)
        outputs, cell_state = tf.nn.dynamic_rnn(
            cell,
            x,
            dtype=tf.float32)

        _ = tf.identity(outputs, name="output")
        _ = tf.identity(cell_state, name="cell_state")

        input_names_with_port = ["input_1:0"]
        feed_dict = {"input_1:0": x_val}

        output_names_with_port = ["output:0", "cell_state:0"]
        self.run_test_case(feed_dict, input_names_with_port, output_names_with_port, rtol=1e-06, atol=1e-07)

    # ==============================================================================================
    # NOTE: the unittest above should be converted into a single LSTM op, while following unittests
    # should be first converted into a Scan op with LSTMBlockCell, then decoupled into several ops.
    # ==============================================================================================

    @check_opset_min_version(8, "Scan")
    def test_single_dynamic_lstm_with_peephole(self):
        units = 5
        batch_size = 6
        x_val = np.array([[1., 1.], [2., 2.], [3., 3.], [4., 4.]], dtype=np.float32)
        x_val = np.stack([x_val] * batch_size)

        x = tf.placeholder(tf.float32, x_val.shape, name="input_1")

        # no scope
        cell = rnn.LSTMBlockCell(units, use_peephole=True)
        outputs, cell_state = tf.nn.dynamic_rnn(
            cell,
            x,
            dtype=tf.float32)

        _ = tf.identity(outputs, name="output")
        _ = tf.identity(cell_state, name="cell_state")

        input_names_with_port = ["input_1:0"]
        feed_dict = {"input_1:0": x_val}

        output_names_with_port = ["output:0", "cell_state:0"]
        self.run_test_case(feed_dict, input_names_with_port, output_names_with_port, rtol=1e-06, atol=1e-07)

    @check_opset_min_version(8, "Scan")
    def test_single_dynamic_lstm_with_cell_clip(self):
        units = 5
        batch_size = 6
        x_val = np.array([[1., 1.], [2., 2.], [3., 3.], [4., 4.]], dtype=np.float32)
        x_val = np.stack([x_val] * batch_size)

        x = tf.placeholder(tf.float32, x_val.shape, name="input_1")

        # no scope
        cell = rnn.LSTMBlockCell(units, cell_clip=0.05)
        outputs, cell_state = tf.nn.dynamic_rnn(
            cell,
            x,
            dtype=tf.float32)

        _ = tf.identity(outputs, name="output")
        _ = tf.identity(cell_state, name="cell_state")

        input_names_with_port = ["input_1:0"]
        feed_dict = {"input_1:0": x_val}

        output_names_with_port = ["output:0", "cell_state:0"]
        self.run_test_case(feed_dict, input_names_with_port, output_names_with_port, rtol=1e-06, atol=1e-07)

    @check_opset_min_version(8, "Scan")
    @check_tf_min_version("1.8")
    def test_attention_wrapper_lstm_encoder(self):
        size = 5
        time_step = 3
        input_size = 4
        attn_size = size

        batch_size = 9

        # shape  [batch size, time step, size]
        # attention_state: usually the output of an RNN encoder.
        # This tensor should be shaped `[batch_size, max_time, ...]`
        encoder_time_step = time_step
        encoder_x_val = np.random.randn(encoder_time_step, input_size).astype('f')
        encoder_x_val = np.stack([encoder_x_val] * batch_size)
        encoder_x = tf.placeholder(tf.float32, encoder_x_val.shape, name="input_1")
        encoder_cell = rnn.LSTMBlockCell(size)
        output, attr_state = tf.nn.dynamic_rnn(encoder_cell, encoder_x, dtype=tf.float32)
        _ = tf.identity(output, name="output_0")
        attention_states = output
        attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(attn_size,
                                                                   attention_states)

        match_input_fn = lambda curr_input, state: tf.concat([curr_input, state], axis=-1)
        cell = rnn.LSTMBlockCell(size)
        match_cell_fw = tf.contrib.seq2seq.AttentionWrapper(cell,
                                                            attention_mechanism,
                                                            attention_layer_size=attn_size,
                                                            cell_input_fn=match_input_fn,
                                                            output_attention=False)

        decoder_time_step = 6
        decoder_x_val = np.random.randn(decoder_time_step, input_size).astype('f')
        decoder_x_val = np.stack([decoder_x_val] * batch_size)

        decoder_x = tf.placeholder(tf.float32, decoder_x_val.shape, name="input_2")
        output, attr_state = tf.nn.dynamic_rnn(match_cell_fw, decoder_x, dtype=tf.float32)

        _ = tf.identity(output, name="output")
        _ = tf.identity(attr_state.cell_state, name="final_state")

        feed_dict = {"input_1:0": encoder_x_val, "input_2:0": decoder_x_val}
        input_names_with_port = ["input_1:0", "input_2:0"]
        output_names_with_port = ["output_0:0", "output:0", "final_state:0"]
        self.run_test_case(feed_dict, input_names_with_port, output_names_with_port, 0.1)

    @check_opset_min_version(8, "Scan")
    def test_multi_rnn_lstm(self):
        units = 5
        batch_size = 6
        x_val = np.array([[1., 1.], [2., 2.], [3., 3.], [4., 4.]], dtype=np.float32)
        x_val = np.stack([x_val] * batch_size)

        x = tf.placeholder(tf.float32, x_val.shape, name="input_1")

        cell_0 = rnn.LSTMBlockCell(units)

        cell_1 = rnn.LSTMBlockCell(units)

        cell_2 = rnn.LSTMBlockCell(units)

        cells = rnn.MultiRNNCell([cell_0, cell_1, cell_2], state_is_tuple=True)
        outputs, cell_state = tf.nn.dynamic_rnn(cells,
                                                x,
                                                dtype=tf.float32)

        _ = tf.identity(outputs, name="output")
        _ = tf.identity(cell_state, name="cell_state")

        input_names_with_port = ["input_1:0"]
        feed_dict = {"input_1:0": x_val}

        output_names_with_port = ["output:0", "cell_state:0"]
        self.run_test_case(feed_dict, input_names_with_port, output_names_with_port, rtol=1e-06, atol=1e-07)

if __name__ == '__main__':
    unittest_main()
