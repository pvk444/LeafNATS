'''
@author Tian Shi
Please contact tshi@vt.edu
'''
import os
import time

import torch
from torch.autograd import Variable

from LeafNATS.playground.pointer_generator_network import modelPointerGenerator
from LeafNATS.modules.nats_embedding import natsEmbedding
from LeafNATS.modules.nats_encoder_rnn import natsEncoder
from LeafNATS.modules.nats_encoder2decoder import natsEncoder2Decoder
from LeafNATS.modules.nats_decoder_pointer_generator import PointerGeneratorDecoder
from LeafNATS.modules.beam_search_trans import fast_beam_search
from LeafNATS.modules.word_copy import word_copy
from LeafNATS.data.data_utils import construct_vocab
from LeafNATS.data.data_summary_ss import *
from LeafNATS.utils.utils import *
'''
pointer generator network
''' 
class modelNatsTransfer(modelPointerGenerator):
    
    def __init__(self, args):
        super(modelNatsTransfer, self).__init__(args=args)
    
    def build_models(self):
        '''
        build all models.
        in this model source and target share embeddings
        '''
        self.base_models['embedding_base'] = natsEmbedding(
            vocab_size = self.batch_data['vocab_size'],
            emb_dim = self.args.emb_dim,
            share_emb_weight = self.args.share_emb_weight
        ).to(self.args.device)
        
        self.base_models['encoder_base'] = natsEncoder(
            emb_dim = self.args.emb_dim,
            hidden_size = self.args.src_hidden_dim,
            rnn_network = self.args.rnn_network,
            device = self.args.device
        ).to(self.args.device)
        
        self.train_models['encoder'] = natsEncoder(
            emb_dim = self.args.src_hidden_dim*2,
            hidden_size = self.args.src_hidden_dim,
            rnn_network = self.args.rnn_network,
            device = self.args.device
        ).to(self.args.device)
        
        self.train_models['encoder2decoder'] = natsEncoder2Decoder(
            src_hidden_size = self.args.src_hidden_dim,
            trg_hidden_size = self.args.trg_hidden_dim,
            rnn_network = self.args.rnn_network
        ).to(self.args.device)
        
        self.train_models['pgdecoder'] = PointerGeneratorDecoder(
            input_size = self.args.emb_dim,
            src_hidden_size = self.args.src_hidden_dim,
            trg_hidden_size = self.args.trg_hidden_dim,
            attn_method = self.args.attn_method,
            repetition = self.args.repetition,
            pointer_net = self.args.pointer_net,
            attn_decoder = self.args.attn_decoder,
            rnn_network = self.args.rnn_network,
            device = self.args.device
        ).to(self.args.device)
        
        # decoder to vocab
        if self.args.share_emb_weight:
            self.train_models['decoder2proj'] = torch.nn.Linear(
                self.args.trg_hidden_dim, self.args.emb_dim, bias=False).to(self.args.device)
        else:
            self.train_models['decoder2vocab'] = torch.nn.Linear(
                self.args.trg_hidden_dim, self.batch_data['vocab_size']).to(self.args.device)
            
    def init_base_model_params(self):
        '''
        Initialize Model Parameters
        '''
        for model_name in self.base_models:
            fl_ = os.path.join(self.args.base_model_dir, model_name+'.model')
            self.base_models[model_name].load_state_dict(torch.load(fl_))

    def build_pipelines(self):
        '''
        here we have all data flow from the input to output
        '''
        src_emb = self.base_models['embedding_base'].get_embedding(self.batch_data['src_var'])
        encoder_hy0, _ = self.base_models['encoder_base'](src_emb)
        encoder_hy, hidden_encoder = self.train_models['encoder'](encoder_hy0)
        hidden_decoder = self.train_models['encoder2decoder'](hidden_encoder)
        
        trg_emb = self.base_models['embedding_base'].get_embedding(self.batch_data['trg_input_var'])
        
        batch_size = self.batch_data['src_var'].size(0)
        src_seq_len = self.batch_data['src_var'].size(1)
        trg_seq_len = trg_emb.size(1)
        if self.args.repetition == 'temporal':
            past_attn = Variable(torch.ones(batch_size, src_seq_len)).to(self.args.device)
        else:
            past_attn = Variable(torch.zeros(batch_size, src_seq_len)).to(self.args.device)
        h_attn = Variable(torch.zeros(batch_size, self.args.trg_hidden_dim)).to(self.args.device)
        p_gen = Variable(torch.zeros(batch_size, trg_seq_len)).to(self.args.device)
        past_dehy = Variable(torch.zeros(1, 1)).to(self.args.device)
        
        trg_h, _, _, attn_, _, p_gen, _, loss_cv = self.train_models['pgdecoder'](
            0, trg_emb, hidden_decoder, h_attn, encoder_hy, past_attn, p_gen, past_dehy)
        
        # prepare output
        trg_h_reshape = trg_h.contiguous().view(
            trg_h.size(0)*trg_h.size(1), trg_h.size(2))
        # consume a lot of memory.
        if self.args.share_emb_weight:
            decoder_proj = self.train_models['decoder2proj'](trg_h_reshape)
            logits_ = self.base_models['embedding_base'].get_decode2vocab(decoder_proj)
        else:
            logits_ = self.train_models['decoder2vocab'](trg_h_reshape)
        logits_ = logits_.view(trg_h.size(0), trg_h.size(1), logits_.size(1))
        logits_ = torch.softmax(logits_, dim=2)
        
        ex_vocab_size = len(self.batch_data['ext_id2oov'])
        vocab_size = len(self.batch_data['vocab2id']) + ex_vocab_size
        if self.args.pointer_net:
            if self.args.oov_explicit:
                logits_ex = Variable(torch.zeros(1, 1, 1)).to(self.args.device)
                logits_ex = logits_ex.repeat(batch_size, trg_seq_len, ex_vocab_size)
                if ex_vocab_size > 0:
                    logits_ = torch.cat((logits_, logits_ex), -1)
                # pointer
                attn_ = attn_.transpose(0, 1)
                # calculate index matrix
                pt_idx = Variable(torch.FloatTensor(torch.zeros(1, 1, 1))).to(self.args.device)
                pt_idx = pt_idx.repeat(batch_size, src_seq_len, vocab_size)
                pt_idx.scatter_(2, self.batch_data['src_var_ex'].unsqueeze(2), 1.0)
                logits_ = p_gen.unsqueeze(2)*logits_ + (1.0-p_gen.unsqueeze(2))*torch.bmm(attn_, pt_idx)
                logits_ = logits_ + 1e-20
            else:
                attn_ = attn_.transpose(0, 1)
                pt_idx = Variable(torch.FloatTensor(torch.zeros(1, 1, 1))).to(self.args.device)
                pt_idx = pt_idx.repeat(batch_size, src_seq_len, vocab_size)
                pt_idx.scatter_(2, self.batch_data['src_var'].unsqueeze(2), 1.0)
                logits_= p_gen.unsqueeze(2)*logits_ + (1.0-p_gen.unsqueeze(2))*torch.bmm(attn_, pt_idx)
        
        weight_mask = torch.ones(vocab_size).to(self.args.device)
        weight_mask[self.batch_data['vocab2id']['<pad>']] = 0
        loss_criterion = torch.nn.NLLLoss(weight=weight_mask).to(self.args.device)

        logits_ = torch.log(logits_)
        loss = loss_criterion(
            logits_.contiguous().view(-1, vocab_size),
            self.batch_data['trg_output_var'].view(-1))
        
        if self.args.repetition == 'asee_train':
            loss = loss + loss_cv[0]
            
        return loss
    
    def test_worker(self, _nbatch):
        '''
        For the beam search in testing.
        '''
        start_time = time.time()
        fout = open(os.path.join(self.args.data_dir, 'nats_results', self.args.file_output), 'w')
        for batch_id in range(_nbatch):
            if self.args.oov_explicit:
                ext_id2oov, src_var, src_var_ex, src_arr, src_msk, trg_arr \
                = process_minibatch_explicit_test(
                    batch_id=batch_id, path_=self.args.data_dir,
                    batch_size=self.args.test_batch_size, vocab2id=self.batch_data['vocab2id'], 
                    src_lens=self.args.src_seq_lens
                )
                src_msk = src_msk.to(self.args.device)
                src_var = src_var.to(self.args.device)
                src_var_ex = src_var_ex.to(self.args.device)
            else:
                src_var, src_arr, src_msk, trg_arr = process_minibatch_test(
                    batch_id=batch_id, path_=self.args.data_dir, 
                    batch_size=self.args.test_batch_size, vocab2id=self.batch_data['vocab2id'], 
                    src_lens=args.src_seq_lens
                )
                ext_id2oov = {}
                src_msk = src_msk.to(self.args.device)
                src_var = src_var.to(self.args.device)
                src_var_ex = src_var.clone()
            self.batch_data['ext_id2oov'] = ext_id2oov
                
            curr_batch_size = src_var.size(0)
            src_text_rep = src_var.unsqueeze(1).clone().repeat(
                1, self.args.beam_size, 1).view(-1, src_var.size(1)).to(self.args.device)
            if self.args.oov_explicit:
                src_text_rep_ex = src_var_ex.unsqueeze(1).clone().repeat(
                    1, self.args.beam_size, 1).view(-1, src_var_ex.size(1)).to(self.args.device)
            else:
                src_text_rep_ex = src_text_rep.clone()
                
            models = {}
            for model_name in self.base_models:
                models[model_name] = self.base_models[model_name]
            for model_name in self.train_models:
                models[model_name] = self.train_models[model_name]
            beam_seq, beam_prb, beam_attn_ = fast_beam_search(
                self.args, models, self.batch_data,
                src_text_rep, src_text_rep_ex, curr_batch_size)
            # copy unknown words
            out_arr = word_copy(
                self.args, beam_seq, beam_attn_, src_msk, src_arr, curr_batch_size, 
                self.batch_data['id2vocab'], self.batch_data['ext_id2oov'])
            for k in range(curr_batch_size):
                fout.write('<sec>'.join([out_arr[k], trg_arr[k]])+'\n')

            end_time = time.time()
            show_progress(batch_id, _nbatch, str((end_time-start_time)/3600)[:8]+"h")
        fout.close()
        