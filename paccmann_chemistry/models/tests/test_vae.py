import logging
import sys
import time
import unittest

import numpy as np
import torch

from paccmann_chemistry.models.vae import (
    StackGRUDecoder, StackGRUEncoder, TeacherVAE
)
from paccmann_chemistry.utils.search import (
    SamplingSearch, GreedySearch, BeamSearch
)
from paccmann_chemistry.models.training import get_data_preparation

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger('test_vae')

# pylint: disable=not-callable, no-member


class TestStackGRUEncoder(unittest.TestCase):
    """Testing the StackGRUEncoder"""

    default_params = {
        'latent_dim': 128,
        'rnn_cell_size': 32,
        'embedding_size': 30,
        'vocab_size': 100,
        'stack_width': 50,
        'stack_depth': 51,
        'n_layers': 3,
        'dropout': .7,
        'batch_size': 32,
        'bidirectional': True,
        'batch_mode': 'padded'
    }

    def assertListsClose(self, var1, var2, rtol=1e-5, atol=1e-7):
        self.assertTrue(np.allclose(var1, var2, rtol=rtol, atol=atol))

    def test__post_gru_reshape(self) -> None:
        """Tests if the reshaping on the hidden layer of the GRU is correct.

        __Note: For more details look at issue #5__
        """
        params = self.default_params
        cell_size = params['rnn_cell_size']
        n_layers = params['n_layers']
        batch_size = params['batch_size']

        gru_stack = StackGRUEncoder(params)

        correct_sample = np.arange(cell_size)  # C

        # Emulate the hidden layer of the GRU
        hidden = np.tile(correct_sample, n_layers)  # LxC
        hidden = hidden.reshape(n_layers, cell_size)  # LxC
        hidden = [torch.Tensor(hidden) for _ in range(batch_size)]
        hidden = torch.stack(hidden, dim=1)  # LxBxC

        hidden = gru_stack._post_gru_reshape(hidden)

        for sample in hidden.unbind():
            self.assertListEqual(sample.tolist(), correct_sample.tolist())

    def test__no_batch_mismatch__in__encoder_train_step(self) -> None:
        """Tests if there is any difference between equal samples in the same
        batch. If so it may indicate some crosstalk between samples in the
        batch
        """
        params = self.default_params
        batch_size = params['batch_size']
        gru_stack = StackGRUEncoder(params)
        gru_stack.eval()

        base_sample = np.arange(10)
        base = [torch.Tensor(base_sample) for _ in range(batch_size)]
        _input = torch.stack(base, dim=1).long()

        mus, logvars = gru_stack.encoder_train_step(_input)

        first_mu = mus[0].tolist()
        for _mu in mus.unbind():
            # NOTE: I assume there may be some tiny numerical differences
            # between the outputs (e.g. roundoff error)
            self.assertTrue(
                np.allclose(_mu.tolist(), first_mu, rtol=1e-5, atol=1e-7)
            )

        first_logvar = logvars[0].tolist()
        for _logvar in logvars.unbind():
            self.assertListsClose(_logvar.tolist(), first_logvar)

    def test__encoding_independent_from_batch_with_stack(self) -> None:
        self._encoding_independent_from_batch(use_stack=True)

    def test__encoding_independent_from_batch_no_stack(self) -> None:
        self._encoding_independent_from_batch(use_stack=False)

    def _encoding_independent_from_batch(self, use_stack) -> None:
        """Test that the results of a model are gonna be consistent
        regadless of the model's batch size"""

        params = self.default_params
        params['batch_size'] = 128
        params['vocab_size'] = 55
        params['use_stack'] = use_stack

        device = torch.device('cpu')

        gru_encoder = StackGRUEncoder(params).to(device)
        state_dict = gru_encoder.state_dict()

        # 2 (start index) + ordered token sequence
        sample = np.concatenate([[2], np.arange(3, 53)])

        def _get_sample_at_batch_size(batch_size):
            """Helper function to iterate over the batches"""
            params['batch_size'] = batch_size
            gru_encoder = StackGRUEncoder(params).to(device)
            gru_encoder.load_state_dict(state_dict)
            gru_encoder = gru_encoder.eval()

            # Setup a batch to be passed into the encoder. This is the same
            # that `training.sequential_data_preparation` does
            batch = np.stack(
                [sample for _ in range(params['batch_size'])], axis=1
            )
            encoder_seq = torch.tensor(batch).long()
            return gru_encoder.encoder_train_step(encoder_seq)[0][0]

        batch_sizes = [1, 2, 4, 12, 55, 128]

        results_by_batches = [
            _get_sample_at_batch_size(b) for b in batch_sizes
        ]

        for i, res1 in enumerate(results_by_batches):
            for j, res2 in enumerate(results_by_batches[i + 1:]):
                self.assertListsClose(res1.tolist(), res2.tolist())


class testTeacherVAE(unittest.TestCase):
    """Testing the TeacherVAE"""

    params = {
        'latent_dim': 24,
        'embedding_size': 16,
        'stack_width': 5,
        'stack_depth': 5,
        'dropout': .7
    }
    use_stacks = [True, False]
    gen_lens = [50]
    bidirectionals = ['True', 'False']
    n_layerss = [2]
    batch_sizes = [8, 128]
    rnns = [20, 128]
    vocab_sizes = [100]
    beam_sizes = [2, 8]
    top_tokenss = [5, 30]
    batch_modes = ['Padded', 'Packed']
    start_index = 2
    stop_index = 3

    def test_speed(self):

        def _update_params():
            self.params.update(
                {
                    'use_stack': self.use_stack,
                    'bidirectional': self.bidirectional,
                    'n_layers': self.n_layers,
                    'batch_size': self.bs,
                    'rnn_cell_size': self.rnn,
                    'vocab_size': self.vocab_size,
                    'batch_mode': self.batch_mode
                }
            )
            return self.params

        def _log():
            logger.info(
                f'\tMode {self.batch_mode}, Stack: {self.use_stack}, '
                f'bidirectional: {self.bidirectional}'
                f' SeqLen:{self.gen_lens[-1]}, '
                f'layers: {self.n_layers}, batch_size:'
                f' {self.bs}, RNN: {self.rnn}, Vocab: {self.vocab_size}\n'
                f'Setup: {self.setup-self.start:.3f}, Encoder:'
                f'{self.enc_t-self.setup:.3f}, Decoder: '
                f'{self.dec_t-self.enc_t:.3f}\n'
            )

        def _log_search(beam=False):
            txt = ''
            if beam:
                txt = f'Beam size: {self.beam_size}, Top-K: {self.top_tokens}'
            logger.info(
                f"Search: {str(self.search).split('.')[-1].split('>')[0][:-1]}"
                f', Generated length: {self.gen_len}, '
                f'{txt} \t (BS:{self.bs}, #Layer: {self.n_layers}, RNN'
                f' {self.rnn})\n Took: {self.gen_t-self.start:.3f}'
            )

        # # Run
        self.batch_mode = 'Padded'
        for self.use_stack in self.use_stacks:
            for self.bidirectional in self.bidirectionals:
                for self.n_layers in self.n_layerss:
                    for self.bs in self.batch_sizes:
                        for self.rnn in self.rnns:
                            for self.vocab_size in self.vocab_sizes:

                                # Update params
                                p = _update_params()
                                enc_in = torch.rand(
                                    self.gen_lens[-1], self.bs
                                ).long()
                                lat = torch.rand(self.bs, p['latent_dim'])
                                prime = torch.Tensor([2])

                                self.start = time.time()
                                enc = StackGRUEncoder(p)
                                dec = StackGRUDecoder(p)
                                vae = TeacherVAE(enc, dec)
                                self.setup = time.time()

                                enc_out = enc.encoder_train_step(enc_in)
                                self.enc_t = time.time()
                                dec_out = vae.decode(lat, enc_in, enc_in)
                                self.dec_t = time.time()
                                _log()

        logger.info(f'***Decoder search tests***')

        def _call_fn(beam=False):
            search = self.search(
                temperature=1,
                beam_width=self.beam_size,
                top_tokens=self.top_tokens
            )
            self.start = time.time()
            gen = self.vae.generate(
                self.lat,
                self.prime,
                self.prime,
                generate_len=self.gen_len,
                search=search
            )
            self.gen_t = time.time()
            _log_search(beam=beam)

        self.bs = self.batch_sizes[0]
        self.n_layers = self.n_layerss[0]
        self.rnn = self.rnns[0]
        self.bidirectional = self.bidirectionals[0]
        self.top_tokens = self.top_tokenss[0]
        self.beam_size = self.beam_sizes[0]
        for self.use_stack in self.use_stacks:
            for self.vocab_size in self.vocab_sizes:
                for self.gen_len in self.gen_lens:
                    for self.search in [
                        GreedySearch, SamplingSearch, BeamSearch
                    ]:

                        p = _update_params()
                        enc = StackGRUEncoder(p)
                        dec = StackGRUDecoder(p)
                        self.vae = TeacherVAE(enc, dec)
                        self.lat = torch.randn(1, self.bs, p['latent_dim'])
                        self.prime = torch.Tensor([self.start_index]).long()

                        if self.search == BeamSearch:
                            for self.beam_size in self.beam_sizes:
                                for self.top_tokens in self.top_tokenss:
                                    _call_fn(beam=True)
                        else:
                            _call_fn(beam=False)
        self.assertTrue(True)

    def test_speed_pack_vs_pad(self):
        logger.info('\nTesting Pack vs Pad')

        n_layerss = [2, 4]
        batch_sizes = [64]
        rnns = [32, 128]
        vocab_sizes = [20, 100]
        batch_modes = ['Padded', 'Packed']

        def _update_params():
            self.params.update(
                {
                    'use_stack': self.use_stack,
                    'bidirectional': self.bidirectional,
                    'n_layers': self.n_layers,
                    'batch_size': self.bs,
                    'rnn_cell_size': self.rnn,
                    'vocab_size': self.vocab_size,
                    'batch_mode': self.batch_mode
                }
            )
            return self.params

        def _log():
            logger.info(
                f'\tMode {self.batch_mode}, Stack: {self.use_stack}, '
                f'bidirectional: {self.bidirectional}'
                f' SeqLen:{self.gen_lens[-1]}, '
                f'layers: {self.n_layers}, batch_size:'
                f' {self.bs}, RNN: {self.rnn}, Vocab: {self.vocab_size}\n'
                f'Setup: {self.setup-self.start:.3f}, '
                f'Encoder: {self.enc_t-self.setup:.3f}, '
                f'Reparam: {self.reparam_t - self.enc_t:.3f} '
                f'Decoder: {self.dec_t-self.reparam_t:.3f}\n'
            )

        # # Run
        self.use_stack = True
        self.bidirectional = True
        for self.batch_mode in batch_modes:
            for self.n_layers in n_layerss:
                for self.bs in batch_sizes:
                    for self.rnn in rnns:
                        for self.vocab_size in vocab_sizes:

                            # Update params
                            p = _update_params()
                            batch = [
                                torch.tensor(
                                    [self.start_index] + [
                                        np.random.randint(4, 20)
                                        for _ in range(
                                            np.random.
                                            randint(3, self.gen_lens[-1])
                                        )
                                    ] + [self.stop_index]
                                ).long() for _ in range(self.bs)
                            ]
                            batch = sorted(
                                batch, key=lambda x: len(x), reverse=True
                            )

                            (enc_in, dec_seq,
                             target_seq) = get_data_preparation(
                                 p['batch_mode']
                             )(
                                 batch,
                                 input_keep=0.8,
                                 start_index=2,
                                 end_index=3,
                                 device=torch.device('cpu')
                             )

                            lat = torch.rand(self.bs, p['latent_dim'])
                            prime = torch.Tensor([2])

                            self.start = time.time()
                            enc = StackGRUEncoder(p)
                            dec = StackGRUDecoder(p)
                            vae = TeacherVAE(enc, dec)
                            self.setup = time.time()
                            enc_out = enc.encoder_train_step(enc_in)
                            self.enc_t = time.time()

                            latent_z = vae.reparameterize(*enc_out
                                                          ).unsqueeze(0)
                            self.reparam_t = time.time()

                            dec_out = vae.decode(latent_z, dec_seq, target_seq)
                            self.dec_t = time.time()
                            _log()
        self.assertTrue(True)
