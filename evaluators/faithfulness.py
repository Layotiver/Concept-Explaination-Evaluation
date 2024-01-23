from .base import BaseEvaluator

import torch
import torch.nn as nn
from logger import logger
import numpy as np
from tqdm import tqdm

class FaithfulnessEvaluator(nn.Module, BaseEvaluator):
    def __init__(
        self, 
        cfg, 
        activation_func, 
        model, 
        concept=None, 
        concept_idx=-1, 
        disturb='ablation', 
        measure_obj='loss', 
        corr_func='cosine', 
        class_idx=0, 
        logits_corr_topk=None
    ):
        nn.Module.__init__(self)
        BaseEvaluator.__init__(self, cfg, activation_func, model)
        self.concept = concept
        self.concept_idx = concept_idx
        self.disturb = disturb
        self.measure_obj = measure_obj
        self.corr_func = corr_func
        self.class_idx = class_idx
        self.logits_corr_topk = logits_corr_topk
        
    @classmethod
    def code(cls):
        return 'faithfulness'
    
    def get_metric(self, eval_tokens):
        
        if self.measure_obj not in ['loss', 'class_logit', 'logits']:
            assert False, "measure_obj must be one of ['loss', 'class_logit', 'logits']."
            
        if self.disturb not in ['ablation', 'gradient', 'replace']:
            assert False, "disturb must be one of ['ablation', 'gradient', 'replace']."
        
        if self.corr_func not in ['cosine', 'KL_div', 'openai_var']:
            assert False, "corr_func must be one of ['cosine', 'KL_div', 'openai_var']."
        
        _, maxlen = eval_tokens.shape[0], eval_tokens.shape[1]
        minibatch = self.cfg['concept_eval_batchsize']
        eval_tokens = eval_tokens.split(minibatch, dim=0)
            
        metrics = []
        concept_acts = []
        params = self.model.parameters()
        optimizer = torch.optim.Adam(params, lr=0.001)
        for tokens in tqdm(eval_tokens, desc='Traverse the evaluation corpus to calculate metrics'):            
            
            concept_act = self.activation_func(tokens, self.model, self.concept, self.concept_idx) # minibatch * maxlen
            concept_acts.append(concept_act.cpu().reshape(minibatch, maxlen).numpy())
            
            if self.disturb == 'gradient':
                if self.measure_obj == 'logits':
                    assert False, "When the disturbance type is 'gradient', the measurement object must be one of ['loss', 'class_logit']."
                elif self.measure_obj == 'loss':
                    grads, hidden_state = self.get_loss_gradient(tokens)
                    metric = (grads @ self.concept.cpu().numpy().T).squeeze() # minibatch * maxlen
                elif self.measure_obj == 'class_logit':
                    grads, hidden_state = self.get_class_logit_gradient(tokens, self.class_idx)
                    metric = (grads @ self.concept.cpu().numpy().T).squeeze() # minibatch * maxlen
                metric = metric[:,:-1]
                optimizer.zero_grad()
                
            elif self.disturb == 'ablation':
                with torch.no_grad():
                    if self.measure_obj == 'logits':
                        metric = self.get_logit_distribution_corr(tokens, 
                                                                concept=self.concept, 
                                                                hook=self.ablation_hook, 
                                                                topk=self.logits_corr_topk, 
                                                                corr_func=self.corr_func) # minibatch * maxlen
                    elif self.measure_obj == 'loss':
                        metric = self.get_loss_diff(tokens, 
                                                    concept=self.concept,
                                                    hook=self.ablation_hook
                                                    ) # minibatch * (maxlen-1)
                    elif self.measure_obj == 'class_logit':
                        metric = self.get_class_logit_diff(tokens,
                                                        concept=self.concept,
                                                        class_idx=self.class_idx,
                                                        hook=self.ablation_hook
                                                        ) # minibatch * maxlen
                
            elif self.disturb == 'replace':
                with torch.no_grad():
                    if self.measure_obj == 'logits':
                        metric = self.get_logit_distribution_corr(tokens, 
                                                                concept=self.concept, 
                                                                hook=self.replacement_hook, 
                                                                topk=self.logits_corr_topk, 
                                                                corr_func=self.corr_func)
                    elif self.measure_obj == 'loss':
                        metric = self.get_loss_diff(tokens, 
                                                    concept=self.concept,
                                                    hook=self.replacement_hook
                                                    )
                    elif self.measure_obj == 'class_logit':
                        metric = self.get_class_logit_diff(tokens,
                                                        concept=self.concept,
                                                        class_idx=self.class_idx,
                                                        hook=self.replacement_hook
                                                        )
            metrics.append(metric)
        
        
        concept_acts = np.concatenate(concept_acts, axis=0)
        metrics = np.concatenate(metrics, axis=0)
        
        if (self.disturb in ['ablation', 'replace']) and (self.measure_obj in ['loss']):
            concept_acts = concept_acts[:,:-1]
        
        pos_act_metric = metrics[concept_acts>0].mean()
        
        pos_act_metric_08max = metrics[concept_acts>0.8*concept_acts.max()].mean()
        
        pos_act_metric_09max = metrics[concept_acts>0.9*concept_acts.max()].mean()
        
        weighted_metric = (metrics * concept_acts).mean()
    
        normed_concept_acts = concept_acts / torch.tensor(concept_acts).norm(p=1, keepdim=True).numpy()
        weighted_normed_metric = (metrics * normed_concept_acts).sum()
            
        softmax_concept_acts = torch.softmax(torch.tensor(concept_acts[concept_acts>0]), dim=-1).numpy()
        weighted_softmax_metric = (metrics[concept_acts>0] * softmax_concept_acts).sum()
            
        logger.info('Faithfulness Metrics ({} {} {} logits_corr_topk={})'.format(
            self.disturb, 
            self.measure_obj, 
            self.corr_func, 
            self.logits_corr_topk
        ))    
        logger.info('avg where concept activation > 0: {:4E}'.format(pos_act_metric))    
        logger.info('avg where concept activation > 0.8 max: {:4E}'.format(pos_act_metric_08max))  
        logger.info('avg where concept activation > 0.9 max: {:4E}'.format(pos_act_metric_09max))  
        logger.info('weighted avg by concept activation: {:4E}'.format(weighted_metric))    
        logger.info('weighted sum by 1-normed concept activation: {:4E}'.format(weighted_normed_metric))  
        logger.info('weighted sum by softmaxed concept activation: {:4E}'.format(weighted_softmax_metric))      
        return metric
        
            