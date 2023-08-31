import argparse
import os
import csv

import yaml
from tqdm import tqdm
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import CosineAnnealingLR

import datasets
import models
import utils
from statistics import mean
import torch
import torch.nn as nn
import torch.distributed as dist


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class BBCEWithLogitLoss(nn.Module):
    '''
    Balanced BCEWithLogitLoss
    '''
    def __init__(self):
        super(BBCEWithLogitLoss, self).__init__()

    def forward(self, pred, gt):
        eps = 1e-10
        count_pos = torch.sum(gt) + eps
        count_neg = torch.sum(1. - gt)
        ratio = count_neg / count_pos
        w_neg = count_pos / (count_pos + count_neg)

        bce1 = nn.BCEWithLogitsLoss(pos_weight=ratio)
        loss = w_neg * bce1(pred, gt)

        return loss

def _iou_loss(pred, target):
    pred = torch.sigmoid(pred)
    inter = (pred * target).sum(dim=(2, 3))
    union = (pred + target).sum(dim=(2, 3)) - inter
    iou = 1 - (inter / union)

    return iou.mean()


def make_data_loader(spec, tag=''):
    if spec is None:
        return None

    dataset = datasets.make(spec['dataset'])
    dataset = datasets.make(spec['wrapper'], args={'dataset': dataset})
   
    log('{} dataset: size={}'.format(tag, len(dataset)))
    for k, v in dataset[0].items():
        if k == 'gt_name':
            continue
        log('  {}: shape={}'.format(k, tuple(v.shape)))

    sampler = torch.utils.data.Sampler(dataset)
    loader = DataLoader(dataset, batch_size=spec['batch_size'],
        shuffle=True, num_workers=6, pin_memory=True)
    return loader


def make_data_loader_no_shuffle(spec, tag=''):
    if spec is None:
        return None

    dataset = datasets.make(spec['dataset'])
    dataset = datasets.make(spec['wrapper'], args={'dataset': dataset})
   
    log('{} dataset: size={}'.format(tag, len(dataset)))
    for k, v in dataset[0].items():
        if k == 'gt_name':
            continue
        log('  {}: shape={}'.format(k, tuple(v.shape)))

    sampler = torch.utils.data.Sampler(dataset)
    loader = DataLoader(dataset, batch_size=spec['batch_size'],
        shuffle=False, num_workers=6, pin_memory=True)
    return loader


def make_data_loaders():
    train_loader = make_data_loader(config.get('train_dataset'), tag='train')
    val_loader = make_data_loader(config.get('val_dataset'), tag='val')
    return train_loader, val_loader


def eval_psnr(loader, model, eval_type=None):
    model.eval()

    
    if eval_type == 'f1':
        metric_fn = utils.calc_f1
        metric1, metric2, metric3, metric4 = 'f1', 'auc', 'none', 'none'
    elif eval_type == 'fmeasure':
        metric_fn = utils.calc_fmeasure
        metric1, metric2, metric3, metric4 = 'f_mea', 'mae', 'none', 'none'
    elif eval_type == 'ber':
        metric_fn = utils.calc_ber
        metric1, metric2, metric3, metric4 = 'shadow', 'non_shadow', 'ber', 'none'
    elif eval_type == 'cod':
        metric_fn = utils.calc_cod
        metric1, metric2, metric3, metric4 = 'sm', 'em', 'wfm', 'mae'
    elif eval_type == 'dice':
        metric_fn = utils.cal_dice_iou
        metric1, metric2, metric3, metric4 = 'dice', 'iou', 'none', 'none'
        
    pbar = tqdm(total=len(loader), leave=False, desc='val')


    pred_list = []
    gt_list = []
    
    with torch.no_grad():
        torch.cuda.empty_cache()
        for batch in loader:
    
            inp = batch['inp'].to(device)
            gt = batch['gt'].to(device)

            pred = torch.sigmoid(model(inp, gt, num_points=1))
    
            pred_list.append(pred)
            gt_list.append(gt)
            if pbar is not None:
                pbar.update(1)

        if pbar is not None:
            pbar.close()

    pred_list = torch.cat(pred_list, 1)
    gt_list = torch.cat(gt_list, 1)
    result1, result2, result3, result4 = metric_fn(pred_list, gt_list)

    return result1, result2, result3, result4, metric1, metric2, metric3, metric4


def prepare_training():
    if config.get('resume') is not None:
        model = models.make(config['model']).to(device)
        optimizer = utils.make_optimizer(
            model.parameters(), config['optimizer'])
        epoch_start = config.get('resume') + 1
    else:
        model = models.make(config['model']).to(device)
        optimizer = utils.make_optimizer(
            model.parameters(), config['optimizer'])
        epoch_start = 1
    max_epoch = config.get('epoch_max')
    lr_scheduler = CosineAnnealingLR(optimizer, max_epoch, eta_min=config.get('lr_min'))
    log('model: #params={}'.format(utils.compute_num_params(model, text=True)))
    return model, optimizer, epoch_start, lr_scheduler


def train(train_loader, model, loss_mode):
    model.train()
    pbar = tqdm(total=len(train_loader), leave=False, desc='train')

    loss_list = []
    for batch in train_loader:
        # for k, v in batch.items():
        #     batch[k] = v.to(device)
        inp = batch['inp'].to(device)
        gt = batch['gt'].to(device)
        
        gt_name = batch['gt_name']
        # print("gt: ", gt_name)
        # model.set_input(inp, gt)
        # model.optimize_parameters()
        model.optimizer.zero_grad()
        pred = model(inp, gt, num_points=40)
        if  loss_mode == 'bce':
            criterionBCE = torch.nn.BCEWithLogitsLoss()
        elif loss_mode == 'bbce':
            criterionBCE = BBCEWithLogitLoss()
        elif loss_mode == 'iou':
            criterionBCE = torch.nn.BCEWithLogitsLoss()

        batch_loss = criterionBCE(pred, gt)
        
        if loss_mode == 'iou':
            batch_loss += _iou_loss(pred, gt)

        batch_loss.backward()
        model.optimizer.step()
        loss_list.append(batch_loss)
        #print('loss: ', batch_loss.item())
        if pbar is not None:
            pbar.update(1)

    if pbar is not None:
        pbar.close()

    loss = [i.item() for i in loss_list]
    return mean(loss)


def main(config_, save_path, args):
    global config, log, writer, log_info
    config = config_
    log = utils.set_save_path(save_path, remove=False)
    with open(os.path.join(save_path, 'config.yaml'), 'w') as f:
        yaml.dump(config, f, sort_keys=False)

    train_loader, val_loader = make_data_loaders()
    if config.get('data_norm') is None:
        config['data_norm'] = {
            'inp': {'sub': [0], 'div': [1]},
            'gt': {'sub': [0], 'div': [1]}
        }

    model, optimizer, epoch_start, lr_scheduler = prepare_training()
    model.optimizer = optimizer
    #model.batch_size = config['train_dataset']['batch_size']
    lr_scheduler = CosineAnnealingLR(model.optimizer, config['epoch_max'], eta_min=config.get('lr_min'))

    model = model.to(device)
   
    sam_checkpoint = torch.load(config['sam_checkpoint'])
    model.load_state_dict(sam_checkpoint, strict=False)
    
    for name, para in model.named_parameters():
        if "image_encoder" in name and "prompt_generator" not in name:
            para.requires_grad_(False)

    model_total_params = sum(p.numel() for p in model.parameters())
    model_grad_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('model_grad_params:' + str(model_grad_params), '\nmodel_total_params:' + str(model_total_params))
    # for name, v in model.named_parameters():
    #     if v.requires_grad:
    #         print(name)

    epoch_max = config['epoch_max']
    epoch_val = config.get('epoch_val')
    max_val_v = -1e18 if config['eval_type'] != 'ber' else 1e8
    best_loss = 1e8
    # best_loss = 1.1492
    # max_val_v = 4.8238
    timer = utils.Timer()
    
    for epoch in range(epoch_start, epoch_max + 1):
        print(f"{epoch} : ")
        t_epoch_start = timer.t()
        loss_mode = config['model']['args']['loss']
        train_loss_G = train(train_loader, model, loss_mode)
        lr_scheduler.step()

       
        with open('./save/training_log.csv', 'a', newline='') as f:
            csv_writer = csv.writer(f)
            csv_writer.writerow([epoch, train_loss_G])
            f.close()
            
        log_info = ['epoch {}/{}'.format(epoch, epoch_max)]
    
        log_info.append('train G: loss={:.4f}'.format(train_loss_G))


        model_spec = config['model']
        model_spec['sd'] = model.state_dict()
        optimizer_spec = config['optimizer']
        optimizer_spec['sd'] = optimizer.state_dict()

        save(config, model, save_path, 'last')
        
        if train_loss_G < best_loss:
            best_loss = train_loss_G
            save(config, model, save_path, 'best_loss')
        
        if epoch_val is not None:
            if epoch % epoch_val == 0:
              #torch.cuda.empty_cache()

              result1, result2, result3, result4, metric1, metric2, metric3, metric4 = eval_psnr(val_loader, model,
                  eval_type=config.get('eval_type'))

              log_info.append('val: {}={:.4f}'.format(metric1, result1))              
              log_info.append('val: {}={:.4f}'.format(metric2, result2))             
              log_info.append('val: {}={:.4f}'.format(metric3, result3))
              log_info.append('val: {}={:.4f}'.format(metric4, result4))

              if config['eval_type'] != 'ber':
                  if result1 > max_val_v:
                      print(f'result1: {result1}')
                      max_val_v = result1
                      save(config, model, save_path, 'best')
              else:
                  if result3 < max_val_v:
                      print(f'result3 : {result3}, max_v: {max_val_v}')
                      max_val_v = result3
                      save(config, model, save_path, 'best')

              t = timer.t()
              prog = (epoch - epoch_start + 1) / (epoch_max - epoch_start + 1)
              t_epoch = utils.time_text(t - t_epoch_start)
              t_elapsed, t_all = utils.time_text(t), utils.time_text(t / prog)
              log_info.append('{} {}/{}'.format(t_epoch, t_elapsed, t_all))

              log(', '.join(log_info))


def save(config, model, save_path, name):
    #print("model name = ", config['model']['name'])
    if config['model']['name'] == 'segformer' or config['model']['name'] == 'setr':
        if config['model']['args']['encoder_mode']['name'] == 'evp':
            prompt_generator = model.encoder.backbone.prompt_generator.state_dict()
            decode_head = model.encoder.decode_head.state_dict()
            torch.save({"prompt": prompt_generator, "decode_head": decode_head},
                       os.path.join(save_path, f"prompt_epoch_{name}.pth"))
        else:
            torch.save(model.state_dict(), os.path.join(save_path, f"model_epoch_{name}.pth"))
    else:
        torch.save(model.state_dict(), os.path.join(save_path, f"model_epoch_{name}.pth"))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default="configs/train/setr/train_setr_evp_cod.yaml")
    parser.add_argument('--name', default=None)
    parser.add_argument('--tag', default=None)
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)

    save_name = args.name
    if save_name is None:
        save_name = '_' + args.config.split('/')[-1][:-len('.yaml')]
    if args.tag is not None:
        save_name += '_' + args.tag
    save_path = os.path.join('./save', save_name)

    main(config, save_path, args=args)