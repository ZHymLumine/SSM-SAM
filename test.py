import argparse
import os

import yaml
import torch
from torch.utils.data import DataLoader
from tqdm import tqdm

import datasets
import models
import utils

from torchvision import transforms
from mmcv.runner import load_checkpoint
import csv

def batched_predict(model, inp, coord, bsize):
    with torch.no_grad():
        model.gen_feat(inp)
        n = coord.shape[1]
        ql = 0
        preds = []
        while ql < n:
            qr = min(ql + bsize, n)
            pred = model.query_rgb(coord[:, ql: qr, :])
            preds.append(pred)
            ql = qr
        pred = torch.cat(preds, dim=1)
    return pred, preds


def tensor2PIL(tensor):
    toPIL = transforms.ToPILImage()
    return toPIL(tensor)


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def eval_psnr(loader, model, data_norm=None, eval_type=None, eval_bsize=None,
              verbose=False):
    model.eval()
    if data_norm is None:
        data_norm = {
            'inp': {'sub': [0], 'div': [1]},
            'gt': {'sub': [0], 'div': [1]}
        }

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
    for threshold in range(10, 255, 10):
        result1, result2, result3, result4 = metric_fn(pred_list, gt_list, threshold)
        print(f'threshold: {threshold}: dice: {result1}')

    return result1, result2, result3, result4


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--model')
    parser.add_argument('--prompt', default='none')
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.load(f, Loader=yaml.FullLoader)
    spec = config['test_dataset']
    dataset = datasets.make(spec['dataset'])
    dataset = datasets.make(spec['wrapper'], args={'dataset': dataset})
    loader = DataLoader(dataset, batch_size=spec['batch_size'], shuffle=False,
                        num_workers=8)

    model = models.make(config['model']).to(device)
    sam_checkpoint = torch.load(args.model, map_location=device)
    model.load_state_dict(sam_checkpoint, strict=False)
    
    metric1, metric2, metric3, metric4 = eval_psnr(loader, model,
                                                   data_norm=config.get('data_norm'),
                                                   eval_type=config.get('eval_type'),
                                                   eval_bsize=config.get('eval_bsize'),
                                                   verbose=True)
    print('metric1: {:.4f}'.format(metric1))
    print('metric2: {:.4f}'.format(metric2))
    print('metric3: {:.4f}'.format(metric3))
    print('metric4: {:.4f}'.format(metric4))

    epoch = config['resume']
    with open('./save/test_log.csv', 'a', newline='') as f:
        csv_writer = csv.writer(f)
        csv_writer.writerow([epoch, metric1, metric2, metric3, metric4])
        f.close()