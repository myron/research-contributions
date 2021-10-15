# Copyright 2020 - 2021 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
import torch
import torch.nn.parallel
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.utils.data.distributed
from monai.inferers import sliding_window_inference
from monai.losses import DiceLoss, DiceCELoss
from monai.metrics import DiceMetric
from monai.utils.enums import MetricReduction
from monai.data import load_decathlon_datalist
from monai.transforms import AsDiscrete,Activations,Compose
from monai import transforms, data
from networks.unetr import UNETR
from trainer import Sampler, run_training
from optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from functools import partial
import argparse

parser = argparse.ArgumentParser(description='UNETR segmentation pipeline')
parser.add_argument('--checkpoint', default=None)
parser.add_argument('--logdir', default=None)
parser.add_argument('--save_checkpoint', action='store_true')
parser.add_argument('--max_epochs', default=5000, type=int)
parser.add_argument('--batch_size', default=4, type=int)
parser.add_argument('--sw_batch_size', default=4, type=int)
parser.add_argument('--optim_lr', default=4e-4, type=float)
parser.add_argument('--optim_name', default='adamw', type=str)
parser.add_argument('--reg_weight', default=1e-5, type=float)
parser.add_argument('--momentum', default=0.99, type=float)
parser.add_argument('--noamp', action='store_true')
parser.add_argument('--val_every', default=100, type=int)
parser.add_argument('--distributed', action='store_true')
parser.add_argument('--world_size', default=1, type=int, help='number of nodes for distributed training')
parser.add_argument('--rank', default=0, type=int, help='node rank for distributed training')
parser.add_argument('--dist-url', default='tcp://127.0.0.1:23456', type=str)
parser.add_argument('--dist-backend', default='nccl', type=str, help='distributed backend')
parser.add_argument('--workers', default=8, type=int)
parser.add_argument('--model_name', default='unetr', type=str)
parser.add_argument('--pos_embedd', default='conv', type=str)
parser.add_argument('--norm_name', default='instance', type=str)
parser.add_argument('--num_heads', default=16, type=int)
parser.add_argument('--mlp_dim', default=3072, type=int)
parser.add_argument('--hidden_size', default=768, type=int)
parser.add_argument('--feature_size', default=16, type=int)
parser.add_argument('--in_channels', default=1, type=int)
parser.add_argument('--out_channels', default=14, type=int)
parser.add_argument('--res_block', action='store_true')
parser.add_argument('--conv_block', action='store_true')
parser.add_argument('--roi_x', default=96, type=int)
parser.add_argument('--roi_y', default=96, type=int)
parser.add_argument('--roi_z', default=96, type=int)
parser.add_argument('--dropout_rate', default=0.0, type=float)
parser.add_argument('--RandFlipd_prob', default=0.2, type=float)
parser.add_argument('--RandRotate90d_prob', default=0.2, type=float)
parser.add_argument('--RandScaleIntensityd_prob', default=0.1, type=float)
parser.add_argument('--RandShiftIntensityd_prob', default=0.1, type=float)
parser.add_argument('--infer_overlap', default=0.5, type=float)
parser.add_argument('--lrschedule', default='warmup_cosine', type=str)
parser.add_argument('--warmup_epochs', default=50, type=int)
parser.add_argument('--resume_ckpt', action='store_true')
parser.add_argument('--pretrained_dir', default=None, type=str)
parser.add_argument('--smooth_dr', default=1e-6, type=float)
parser.add_argument('--smooth_nr', default=0.0, type=float)


def main():
    args = parser.parse_args()
    args.amp = not args.noamp
    args.logdir = './runs/' + args.logdir
    print("MAIN Argument values:")
    for k, v in vars(args).items():
        print(k, '=>', v)
    print('-----------------')
    if args.distributed:
        args.ngpus_per_node = torch.cuda.device_count()
        print('Found total gpus', args.ngpus_per_node)
        args.world_size = args.ngpus_per_node * args.world_size
        mp.spawn(main_worker,
                 nprocs=args.ngpus_per_node,
                 args=(args,))
    else:
        main_worker(gpu=0, args=args)

def main_worker(gpu, args):

    if args.distributed:
        torch.multiprocessing.set_start_method('fork', force=True)
    np.set_printoptions(formatter={'float': '{: 0.3f}'.format}, suppress=True)
    args.gpu = gpu
    if args.distributed:
        args.rank = args.rank * args.ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend,
                                init_method=args.dist_url,
                                world_size=args.world_size,
                                rank=args.rank)
    torch.cuda.set_device(args.gpu)
    torch.backends.cudnn.benchmark = True

    print(args.rank, ' gpu', args.gpu)
    if args.rank == 0:
        print('Batch size is:', args.batch_size, 'epochs', args.max_epochs)
    inf_size = roi_size = [args.roi_x, args.roi_y, args.roi_x]
    data_dir ='/dataset/dataset0'
    datalist_json = data_dir + '/dataset_0.json'

    train_transform = transforms.Compose(
        [
            transforms.LoadImaged(keys=["image", "label"]),
            transforms.AddChanneld(keys=["image", "label"]),
            transforms.Orientationd(keys=["image", "label"],
                                    axcodes="RAS"),
            transforms.Spacingd(keys=["image", "label"],
                                pixdim=(1.5, 1.5, 2.0),
                                mode=("bilinear", "nearest")),
            transforms.ScaleIntensityRanged(keys=["image"],
                                            a_min=-175,
                                            a_max=250,
                                            b_min=0.0,
                                            b_max=1.0,
                                            clip=True),
            transforms.CropForegroundd(keys=["image", "label"], source_key="image"),
            transforms.RandCropByPosNegLabeld(
                keys=["image", "label"],
                label_key="label",
                spatial_size=(args.roi_x, args.roi_y, args.roi_x),
                pos=1,
                neg=1,
                num_samples=4,
                image_key="image",
                image_threshold=0,
            ),
            transforms.RandFlipd(keys=["image", "label"],
                                 prob=args.RandFlipd_prob,
                                 spatial_axis=0),
            transforms.RandFlipd(keys=["image", "label"],
                                 prob=args.RandFlipd_prob,
                                 spatial_axis=1),
            transforms.RandFlipd(keys=["image", "label"],
                                 prob=args.RandFlipd_prob,
                                 spatial_axis=2),
            transforms.RandRotate90d(
                keys=["image", "label"],
                prob=args.RandRotate90d_prob,
                max_k=3,
            ),
            transforms.RandScaleIntensityd(keys="image",
                                           factors=0.1,
                                           prob=args.RandScaleIntensityd_prob),
            transforms.RandShiftIntensityd(keys="image",
                                           offsets=0.1,
                                           prob=args.RandShiftIntensityd_prob),
            transforms.ToTensord(keys=["image", "label"]),
        ]
    )
    val_transform = transforms.Compose(
        [
            transforms.LoadImaged(keys=["image", "label"]),
            transforms.AddChanneld(keys=["image", "label"]),
            transforms.Orientationd(keys=["image", "label"],
                                    axcodes="RAS"),
            transforms.Spacingd(keys=["image", "label"],
                                pixdim=(1.5, 1.5, 2.0),
                                mode=("bilinear", "nearest")),
            transforms.ScaleIntensityRanged(keys=["image"],
                                            a_min=-175,
                                            a_max=250,
                                            b_min=0.0,
                                            b_max=1.0,
                                            clip=True),
            transforms.CropForegroundd(keys=["image", "label"], source_key="image"),
            transforms.ToTensord(keys=["image", "label"]),
        ]
    )
    if (args.model_name is None) or args.model_name == 'unetr':
        model = UNETR(
            in_channels=args.in_channels,
            out_channels=args.out_channels,
            img_size=(args.roi_x, args.roi_y, args.roi_z),
            feature_size=args.feature_size,
            hidden_size=args.hidden_size,
            mlp_dim=args.mlp_dim,
            num_heads=args.num_heads,
            pos_embed=args.pos_embedd,
            norm_name=args.norm_name,
            conv_block=True,
            res_block=True,
            dropout_rate=args.dropout_rate)
        if args.resume_ckpt:
            model_dict = torch.load(args.pretrained_dir)
            model.load_state_dict(model_dict['state_dict'])
            print('Use pretrained weights')
        
    else:
        raise ValueError('Unsupported model ' + str(args.model_name))

    dice_loss = DiceCELoss(to_onehot_y=True,
                           softmax=True,
                           squared_pred=True,
                           smooth_nr=args.smooth_nr,
                           smooth_dr=args.smooth_dr)
    post_label = AsDiscrete(to_onehot=True,
                            n_classes=args.out_channels)
    post_pred = AsDiscrete(argmax=True,
                           to_onehot=True,
                           n_classes=args.out_channels)
    dice_acc = DiceMetric(include_background=True,
                          reduction=MetricReduction.MEAN,
                          get_not_nans=True)
    datalist = load_decathlon_datalist(datalist_json,
                                       True,
                                       "training",
                                       base_dir=data_dir)
    val_files = load_decathlon_datalist(datalist_json,
                                        True,
                                        "validation",
                                        base_dir=data_dir)
    print('Crop size', roi_size)
    print('train_files files', len(datalist), 'validation files', len(val_files))
    train_ds = data.CacheDataset(
        data=datalist,
        transform=train_transform,
        cache_num=24,
        cache_rate=1.0,
        num_workers=args.workers,
    )
    train_sampler = Sampler(train_ds) if args.distributed else None
    train_loader = data.DataLoader(train_ds,
                                   batch_size=args.batch_size,
                                   shuffle=(train_sampler is None),
                                   num_workers=args.workers,
                                   sampler=train_sampler,
                                   pin_memory=True)

    val_ds = data.Dataset(data=val_files, transform=val_transform)
    val_sampler = Sampler(val_ds, shuffle=False) if args.distributed else None
    val_loader = data.DataLoader(val_ds,
                                 batch_size=1,
                                 shuffle=False,
                                 num_workers=args.workers,
                                 sampler=val_sampler,
                                 pin_memory=True)

    model_inferer = partial(sliding_window_inference,
                            roi_size=inf_size,
                            sw_batch_size=args.sw_batch_size,
                            predictor=model,
                            overlap=args.infer_overlap)

    pytorch_total_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('Total parameters count', pytorch_total_params)

    best_acc = 0
    start_epoch = 0

    if args.checkpoint is not None:
        checkpoint = torch.load(args.checkpoint, map_location='cpu')
        from collections import OrderedDict
        new_state_dict = OrderedDict()
        for k, v in checkpoint['state_dict'].items():
            new_state_dict[k.replace('backbone.','')] = v
        model.load_state_dict(new_state_dict, strict=False)
        if 'epoch' in checkpoint:
            start_epoch = checkpoint['epoch']
        if 'best_acc' in checkpoint:
            best_acc = checkpoint['best_acc']
        print("=> loaded checkpoint '{}' (epoch {}) (bestacc {})".format(args.checkpoint, start_epoch, best_acc))

    model.cuda(args.gpu)

    if args.distributed:
        torch.cuda.set_device(args.gpu)
        if args.norm_name == 'batch':
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
        model.cuda(args.gpu)
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.gpu],
                                                          output_device=args.gpu,
                                                          find_unused_parameters=True)
    if args.optim_name == 'adam':
        optimizer = torch.optim.Adam(model.parameters(),
                                     lr=args.optim_lr,
                                     weight_decay=args.reg_weight)
    elif args.optim_name == 'adamw':
        optimizer = torch.optim.AdamW(model.parameters(),
                                      lr=args.optim_lr,
                                      weight_decay=args.reg_weight)
    elif args.optim_name == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(),
                                    lr=args.optim_lr,
                                    momentum=args.momentum,
                                    nesterov=True,
                                    weight_decay=args.reg_weight)
    else:
        raise ValueError('Unsupported Optimization Procedure: ' + str(args.optim_name))

    if args.lrschedule == 'warmup_cosine':
        scheduler = LinearWarmupCosineAnnealingLR(optimizer,
                                                  warmup_epochs=args.warmup_epochs,
                                                  max_epochs=args.max_epochs)
    elif args.lrschedule == 'cosine_anneal':
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,
                                                               T_max=args.max_epochs)
        if args.checkpoint is not None:
            scheduler.step(epoch=start_epoch)
    else:
        scheduler = None
    accuracy = run_training(model=model,
                            train_loader=train_loader,
                            val_loader=val_loader,
                            optimizer=optimizer,
                            loss_func=dice_loss,
                            acc_func=dice_acc,
                            args=args,
                            model_inferer=model_inferer,
                            scheduler=scheduler,
                            start_epoch=start_epoch,
                            post_label=post_label,
                            post_pred=post_pred)
    return accuracy

if __name__ == '__main__':
    main()
