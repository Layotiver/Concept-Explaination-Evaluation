from transformer_lens import utils
import torch
import json
import datasets
import numpy as np
import random
import os
import torch.nn.functional as F
from transformers import LlamaConfig, LlamaForCausalLM, AutoTokenizer
from accelerate import init_empty_weights, load_checkpoint_and_dispatch
from transformer_lens import HookedTransformer

DTYPES = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}

def set_seed(seed):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    
def run_with_cache_onesentence(
        *model_args,
        model,
        names_filter,
        device=None,
        remove_batch_dim=False,
        incl_bwd=True,
        reset_hooks_end=True,
        clear_contexts=False,
        seq_len=0,
        logit_token_idx=0,
        **model_kwargs,
    ):
    cache_dict, fwd, bwd = model.get_caching_hooks(
        names_filter, incl_bwd, device, remove_batch_dim=remove_batch_dim
    )

    with model.hooks(
        fwd_hooks=fwd,
        bwd_hooks=bwd,
        reset_hooks_end=reset_hooks_end,
        clear_contexts=clear_contexts,
    ):
        model_out = model(*model_args, **model_kwargs)
        last_token_logit = model_out[:, seq_len-1, :]
        if incl_bwd:
            last_token_logit[0, logit_token_idx].backward()

    return model_out, cache_dict
    
def load_model(args, device_list=None):
    model_name = args.model_to_interpret
    model_path = args.model_dir
    device = args.device
    n_devices = args.n_devices
    
    if 'llama' in model_name.lower():
        config = LlamaConfig.from_pretrained(model_path)
        with init_empty_weights():
            model_config = LlamaForCausalLM._from_config(config) 
        
        device_map = {}
        if device_list != None:
            device_list = [('cuda:' + str(i)) for i in device_list]
        elif device == 'cpu':
            device_list = ['cpu']
        else:
            start_idx = int(device.split(':')[-1])
            device_list = [('cuda:' + str(i)) for i in range(start_idx, start_idx + n_devices)]
        print(device_list)
        
        device_map['model.embed_tokens.weight'] = device_list[0]
        device_map['model.norm.weight'] = device_list[-1]
        device_map['lm_head.weight'] = device_list[-1]
        for i in range(32):
            device_map['model.layers.'+str(i)+'.self_attn'] = device_list[i // (32 // len(device_list) + 1)]
            device_map['model.layers.'+str(i)+'.mlp'] = device_list[i // (32 // len(device_list) + 1)]
            device_map['model.layers.'+str(i)+'.input_layernorm'] = device_list[i // (32 // len(device_list) + 1)]
            device_map['model.layers.'+str(i)+'.post_attention_layernorm'] = device_list[i // (32 // len(device_list) + 1)]
        
        print(device_map)
        hf_model = load_checkpoint_and_dispatch(
            model_config, checkpoint=model_path, device_map=device_map
        )
        hf_model.eval()
        hf_model.requires_grad_(True)
        tokenizer = AutoTokenizer.from_pretrained(model_path)

        model = HookedTransformer.from_pretrained(model_name,
                                                  n_devices=n_devices,
                                                  device=device,
                                                  fold_value_biases=False,
                                                  fold_ln=False, 
                                                  center_writing_weights=False, 
                                                  center_unembed=False, 
                                                  hf_model=hf_model, 
                                                  tokenizer=tokenizer)
    elif 'pythia' in model_name.lower():
        if model_path != '':
            model = HookedTransformer.from_pretrained(model_path).to(device)
        else:
            model = HookedTransformer.from_pretrained(model_name).to(device)
            
    elif 'gpt' in model_name.lower():
        pass
    
    else:
        assert False, 'This type of model is not supported yet.'
    
    return model
    
def load_dataset(data_dir, dataset_name, data_from_hf):
    if data_from_hf:
        if not os.path.exists(data_dir + dataset_name):
            data = datasets.load_dataset("NeelNanda/pile-tokenized-10b", split="train", cache_dir=data_dir)
            data.save_to_disk(os.path.join(data_dir, dataset_name))
        data = datasets.load_from_disk(data_dir + dataset_name)
    else:
        ...
    return data


def post_init_cfg(cfg):
    cfg["model_batch_size"] = cfg["batch_size"] // cfg["seq_len"] # * 16
    cfg["buffer_size"] = cfg["batch_size"] * cfg["buffer_mult"]
    cfg["buffer_batches"] = cfg["buffer_size"] // cfg["seq_len"]
    cfg["act_name"] = utils.get_act_name(cfg["site"], cfg['layer'], cfg['layer_type'], cfg['name_only'])
    cfg["dict_size"] = cfg["act_size"] * cfg["dict_mult"]
    return cfg

def arg_parse_update_cfg(default_cfg, parser):
    """
    Helper function to take in a dictionary of arguments, convert these to command line arguments, look at what was passed in, and return an updated dictionary.
    """
    cfg = dict(default_cfg)
    for key, value in default_cfg.items():
        if type(value) == bool:
            if value:
                parser.add_argument(f"--{key}", action="store_false")
            else:
                parser.add_argument(f"--{key}", action="store_true")
        elif value is None:
            continue
        else:
            parser.add_argument(f"--{key}", type=type(value), default=value)
    args = parser.parse_args()
    parsed_args = vars(args)
    cfg.update(parsed_args)
    
    return cfg, args

def process_cfg(cfg, model_to_interpret):
    d_model = model_to_interpret.cfg.d_model
    d_mlp = model_to_interpret.cfg.d_mlp
    d_model = model_to_interpret.cfg.d_model
    
    cfg['d_mlp'] = d_mlp
    cfg['d_model'] = d_model
    
    if cfg['site'] == 'mlp_post':
        cfg["dict_size"] = cfg["dict_mult"] * cfg["d_mlp"]
        cfg["act_size"] = cfg["d_mlp"]
    else:
        cfg["dict_size"] = cfg["dict_mult"] * cfg["d_model"]
        cfg["act_size"] = cfg["d_model"]
        
    cfg["num_batches"] = cfg["num_tokens"] // cfg["batch_size"] 
    cfg = post_init_cfg(cfg)
    
    print("Updated config")
    
    return cfg