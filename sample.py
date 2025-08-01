import os
import torch
import argparse
import numpy as np
from math import ceil
from unet import Unet
from collections import defaultdict
from dataloader_cifar import transback
from diffusion import GaussianDiffusion
from torchvision.utils import save_image
from utils import get_named_beta_schedule
from dataloader_cifar import load_data, transback
from torch import nn
from embedding import ConditionalEmbedding
from torch.nn.parallel import DistributedDataParallel as DDP
from resize_right import resize
from torch.distributed import get_rank, init_process_group, destroy_process_group, all_gather, get_world_size
data_dir = ''
@torch.no_grad()

def sample(params:argparse.Namespace):
    assert params.genbatch % (torch.cuda.device_count() * params.clsnum) == 0 , 'please re-set your genbatch!!!'

    init_process_group(backend="nccl")
 
    
    local_rank = get_rank()

    device = torch.device("cuda", local_rank)
   
    net = Unet(
                in_ch = params.inch,
                mod_ch = params.modch,
                out_ch = params.outch,
                ch_mul = params.chmul,
                num_res_blocks = params.numres,
                cdim = params.cdim,
                use_conv=params.useconv,
                droprate = params.droprate,
                # num_heads = params.numheads,
                dtype=params.dtype
            ).to(device)
    checkpoint = torch.load(os.path.join(params.moddir, f'ckpt_{params.epoch}_checkpoint.pt'), map_location='cpu')
    net.load_state_dict(checkpoint['net'])
   
    cemblayer = ConditionalEmbedding(5, params.cdim, params.cdim).to(device)
    cemblayer.load_state_dict(checkpoint['cemblayer'])
   
    betas = get_named_beta_schedule(num_diffusion_timesteps = params.T)
    diffusion = GaussianDiffusion(
                    dtype = params.dtype,
                    model = net,
                    betas = betas,
                    w = params.w,
                    v = params.v,
                    device = device
                )
    
    diffusion.model = DDP(
                            diffusion.model,
                            device_ids = [local_rank],
                            output_device = local_rank
                        )
    cemblayer = DDP(
                    cemblayer,
                    device_ids = [local_rank],
                    output_device = local_rank
                )
 
    def prepare_conditions(dataset_path, clsnum, genbatch_per_cls, device):
   
        cond_loader, _ = load_data(1, 0, dataset_path)  
        dataset = cond_loader.dataset
        
     
        class_indices = defaultdict(list)
        for idx, (_, label) in enumerate(dataset):
            class_indices[label].append(idx)
        
     
        conditions = []
        labels = []
        maxpool = nn.MaxPool2d(2, 2).to(device)
        
        for cls in range(clsnum):
            indices = class_indices.get(cls, [])
            if not indices:
                raise ValueError(f"Class {cls} not found in training set")
                
            for _ in range(genbatch_per_cls):
                
                rand_idx = np.random.choice(indices)
                sample, _ = dataset[rand_idx]
                sample = sample.unsqueeze(0).to(device) 
                
              
                with torch.no_grad():
                    
                    downsampled = resize(sample, out_shape=(32, 32))
                    resized = resize(downsampled, out_shape=(64, 64))
                    
                conditions.append(resized)
                labels.append(cls)
        
        return torch.cat(conditions, dim=0), torch.tensor(labels).to(device)

    
    diffusion.model.eval()
    cemblayer.eval()
    cnt = torch.cuda.device_count()
    conditions, lab = prepare_conditions(
         dataset_path=data_dir,
         clsnum=params.clsnum,
         genbatch_per_cls=params.genbatch // params.clsnum,
         device=device
         )
    
    if params.fid:
        numloop = ceil(params.genum  / params.genbatch)
    else:
        numloop = 1
    each_device_batch = params.genbatch // cnt
    
    cemb = cemblayer(lab, conditions)
    genshape = (each_device_batch, 1, 64, 64)
    all_samples = []
    if local_rank == 0:
        print(numloop)
    for _ in range(numloop):
        if params.ddim:
            generated = diffusion.ddim_sample(genshape, params.num_steps, params.eta, params.select, cemb = cemb)
        else:
            generated = diffusion.sample(genshape, cemb = cemb)
     
        img = transback(generated)
        img = img.reshape(params.clsnum, each_device_batch // params.clsnum, 1, 64, 64).contiguous()
        gathered_samples = [torch.zeros_like(img) for _ in range(get_world_size())]
        all_gather(gathered_samples, img)
        all_samples.extend([img.cpu() for img in gathered_samples])
    samples = torch.concat(all_samples, dim = 1).reshape(params.genbatch * numloop, 1, 64, 64)
    if local_rank == 0:
        print(samples.shape)
      
        if params.fid:
            samples = samples.clamp(0, 255).to(torch.uint8)
            samples = samples.permute(0, 2, 3, 1).numpy()[:params.genum]
            print(samples.shape)
            np.savez(os.path.join(params.samdir, f'sample_{samples.shape[0]}_diffusion_{params.epoch}_{params.w}_ddim.npz'),samples)
        else:
            save_image(samples, os.path.join(params.samdir, f'sample_{params.epoch}_pict_{params.w}.png'), nrow = params.genbatch // params.clsnum)
    destroy_process_group()
def main():
    # several hyperparameters for models
    parser = argparse.ArgumentParser(description='test for diffusion model')

    parser.add_argument('--genbatch',type=int,default=5,help='batch size for sampling process')
    parser.add_argument('--T',type=int,default=1500,help='timesteps for Unet model')
    parser.add_argument('--dtype',default=torch.float32)
    parser.add_argument('--w',type=float,default=3,help='hyperparameters for classifier-free guidance strength')
    parser.add_argument('--v',type=float,default=0.3,help='hyperparameters for the variance of posterior distribution')
    parser.add_argument('--epoch',type=int,default=1500,help='epochs for loading models')
    parser.add_argument('--cdim',type=int,default=6,help='dimension of conditional embedding')
   
    parser.add_argument('--label',type=str,default='range',help='labels of generated images')
    parser.add_argument('--moddir',type=str,default='model',help='model addresses')
    parser.add_argument('--samdir',type=str,default='sample',help='sample addresses')
    parser.add_argument('--inch',type=int,default=1,help='input channels for Unet model')
    parser.add_argument('--modch',type=int,default=64,help='model channels for Unet model')

    parser.add_argument('--outch',type=int,default=1,help='output channels for Unet model')
    parser.add_argument('--chmul',type=list,default=[1,2,2,2],help='architecture parameters training Unet model')
    parser.add_argument('--numres',type=int,default=2,help='number of resblocks for each block in Unet model')
    parser.add_argument('--useconv',type=bool,default=True,help='whether use convlution in downsample')

    parser.add_argument('--droprate',type=float,default=0.1,help='dropout rate for model')
    parser.add_argument('--clsnum',type=int,default=5,help='num of label classes')
    parser.add_argument('--fid',type=lambda x:(str(x).lower() in ['true','1', 'yes']),default=False,help='generate samples used for quantative evaluation')
    parser.add_argument('--genum',type=int,default=25,help='num of generated samples')
    parser.add_argument('--num_steps',type=int,default=50,help='sampling steps for DDIM')
    parser.add_argument('--eta',type=float,default=0,help='eta for variance during DDIM sampling process')
    parser.add_argument('--select',type=str,default='linear',help='selection stragies for DDIM')
    parser.add_argument('--ddim',type=lambda x:(str(x).lower() in ['true','1', 'yes']),default=False,help='whether to use ddim')
    parser.add_argument('--local_rank',default=-1,type=int,help='node rank for distributed training')
    args = parser.parse_args()
    sample(args)

if __name__ == '__main__':
    main()
