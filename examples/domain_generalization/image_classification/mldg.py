"""
@author: Baixu Chen
@contact: cbx_99_hasta@outlook.com
"""
import random
import time
import warnings
import argparse
import shutil
import os.path as osp

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.optim import SGD
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader
import torch.nn.functional as F
import higher

import utils
from tllib.utils.data import ForeverDataIterator
from tllib.utils.metric import accuracy
from tllib.utils.meter import AverageMeter, ProgressMeter
from tllib.utils.logger import CompleteLogger
from tllib.utils.analysis import tsne, a_distance

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def main(args: argparse.Namespace):
    logger = CompleteLogger(args.log, args.phase)
    print(args)

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    cudnn.benchmark = True

    # Data loading code
    train_transform = utils.get_train_transform(args.train_resizing, random_horizontal_flip=True,
                                                random_color_jitter=True, random_gray_scale=True)
    val_transform = utils.get_val_transform(args.val_resizing)
    print("train_transform: ", train_transform)
    print("val_transform: ", val_transform)

    train_dataset, num_classes = utils.get_dataset(dataset_name=args.data, root=args.root, task_list=args.sources,
                                                   split='train', download=True, transform=train_transform,
                                                   seed=args.seed)
    n_domains_per_batch = args.n_support_domains + args.n_query_domains
    sampler = utils.RandomDomainSampler(train_dataset, args.batch_size, n_domains_per_batch=n_domains_per_batch)
    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                              sampler=sampler, drop_last=True)
    val_dataset, _ = utils.get_dataset(dataset_name=args.data, root=args.root, task_list=args.sources, split='val',
                                       download=True, transform=val_transform, seed=args.seed)
    val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    test_dataset, _ = utils.get_dataset(dataset_name=args.data, root=args.root, task_list=args.targets, split='test',
                                        download=True, transform=val_transform, seed=args.seed)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers)
    print("train_dataset_size: ", len(train_dataset))
    print('val_dataset_size: ', len(val_dataset))
    print("test_dataset_size: ", len(test_dataset))
    train_iter = ForeverDataIterator(train_loader)

    # create model
    print("=> using pre-trained model '{}'".format(args.arch))
    backbone = utils.get_model(args.arch)
    pool_layer = nn.Identity() if args.no_pool else None
    classifier = utils.ImageClassifier(backbone, num_classes, freeze_bn=args.freeze_bn, dropout_p=args.dropout_p,
                                       finetune=args.finetune, pool_layer=pool_layer).to(device)

    # define optimizer and lr scheduler
    optimizer = SGD(classifier.get_parameters(base_lr=args.lr), args.lr, momentum=args.momentum, weight_decay=args.wd,
                    nesterov=True)
    lr_scheduler = CosineAnnealingLR(optimizer, args.epochs * args.iters_per_epoch)

    # resume from the best checkpoint
    if args.phase != 'train':
        checkpoint = torch.load(logger.get_checkpoint_path('best'), map_location='cpu')
        classifier.load_state_dict(checkpoint)

    # analysis the model
    if args.phase == 'analysis':
        # extract features from both domains
        feature_extractor = nn.Sequential(classifier.backbone, classifier.pool_layer, classifier.bottleneck).to(device)
        source_feature = utils.collect_feature(val_loader, feature_extractor, device, max_num_features=100)
        target_feature = utils.collect_feature(test_loader, feature_extractor, device, max_num_features=100)
        print(len(source_feature), len(target_feature))
        # plot t-SNE
        tSNE_filename = osp.join(logger.visualize_directory, 'TSNE.png')
        tsne.visualize(source_feature, target_feature, tSNE_filename)
        print("Saving t-SNE to", tSNE_filename)
        # calculate A-distance, which is a measure for distribution discrepancy
        A_distance = a_distance.calculate(source_feature, target_feature, device)
        print("A-distance =", A_distance)
        return

    if args.phase == 'test':
        acc1 = utils.validate(test_loader, classifier, args, device)
        print(acc1)
        return

    # start training
    best_val_acc1 = 0.
    best_test_acc1 = 0.
    for epoch in range(args.epochs):
        print(lr_scheduler.get_lr())
        # train for one epoch
        train(train_iter, classifier, optimizer, lr_scheduler, epoch, n_domains_per_batch, args)

        # evaluate on validation set
        print("Evaluate on validation set...")
        acc1 = utils.validate(val_loader, classifier, args, device)

        # remember best acc@1 and save checkpoint
        torch.save(classifier.state_dict(), logger.get_checkpoint_path('latest'))
        if acc1 > best_val_acc1:
            shutil.copy(logger.get_checkpoint_path('latest'), logger.get_checkpoint_path('best'))
        best_val_acc1 = max(acc1, best_val_acc1)

        # evaluate on test set
        print("Evaluate on test set...")
        best_test_acc1 = max(best_test_acc1, utils.validate(test_loader, classifier, args, device))

    # evaluate on test set
    classifier.load_state_dict(torch.load(logger.get_checkpoint_path('best')))
    acc1 = utils.validate(test_loader, classifier, args, device)
    print("test acc on test set = {}".format(acc1))
    print("oracle acc on test set = {}".format(best_test_acc1))
    logger.close()


def random_split(x_list, labels_list, n_domains_per_batch, n_support_domains):
    assert n_support_domains < n_domains_per_batch

    support_domain_idxes = random.sample(range(n_domains_per_batch), n_support_domains)
    support_domain_list = [(x_list[idx], labels_list[idx]) for idx in range(n_domains_per_batch) if
                           idx in support_domain_idxes]
    query_domain_list = [(x_list[idx], labels_list[idx]) for idx in range(n_domains_per_batch) if
                         idx not in support_domain_idxes]

    return support_domain_list, query_domain_list


def train(train_iter: ForeverDataIterator, model, optimizer, lr_scheduler: CosineAnnealingLR, epoch: int,
          n_domains_per_batch: int, args: argparse.Namespace):
    batch_time = AverageMeter('Time', ':4.2f')
    data_time = AverageMeter('Data', ':3.1f')
    losses = AverageMeter('Loss', ':3.2f')
    cls_accs = AverageMeter('Cls Acc', ':3.1f')

    progress = ProgressMeter(
        args.iters_per_epoch,
        [batch_time, data_time, losses, cls_accs],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i in range(args.iters_per_epoch):
        x, labels, _ = next(train_iter)
        x = x.to(device)
        labels = labels.to(device)

        # measure data loading time
        data_time.update(time.time() - end)

        # split into support domain and query domain
        x_list = x.chunk(n_domains_per_batch, dim=0)
        labels_list = labels.chunk(n_domains_per_batch, dim=0)
        support_domain_list, query_domain_list = random_split(x_list, labels_list, n_domains_per_batch,
                                                              args.n_support_domains)
        # clear grad
        optimizer.zero_grad()

        # compute output
        with higher.innerloop_ctx(model, optimizer, copy_initial_weights=False) as (inner_model, inner_optimizer):
            # perform inner optimization
            for _ in range(args.inner_iters):
                loss_inner = 0
                for (x_s, labels_s) in support_domain_list:
                    y_s, _ = inner_model(x_s)
                    # normalize loss by support domain num
                    loss_inner += F.cross_entropy(y_s, labels_s) / args.n_support_domains

                inner_optimizer.step(loss_inner)

            # calculate outer loss
            loss_outer = 0
            cls_acc = 0

            # loss on support domains
            for (x_s, labels_s) in support_domain_list:
                y_s, _ = model(x_s)
                # normalize loss by support domain num
                loss_outer += F.cross_entropy(y_s, labels_s) / args.n_support_domains

            # loss on query domains
            for (x_q, labels_q) in query_domain_list:
                y_q, _ = inner_model(x_q)
                # normalize loss by query domain num
                loss_outer += F.cross_entropy(y_q, labels_q) * args.trade_off / args.n_query_domains
                cls_acc += accuracy(y_q, labels_q)[0] / args.n_query_domains

        # update statistics
        losses.update(loss_outer.item(), args.batch_size)
        cls_accs.update(cls_acc.item(), args.batch_size)

        # compute gradient and do SGD step
        loss_outer.backward()
        optimizer.step()
        lr_scheduler.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Meta Learning for Domain Generalization')
    # dataset parameters
    parser.add_argument('root', metavar='DIR',
                        help='root path of dataset')
    parser.add_argument('-d', '--data', metavar='DATA', default='PACS',
                        help='dataset: ' + ' | '.join(utils.get_dataset_names()) +
                             ' (default: PACS)')
    parser.add_argument('-s', '--sources', nargs='+', default=None,
                        help='source domain(s)')
    parser.add_argument('-t', '--targets', nargs='+', default=None,
                        help='target domain(s)')
    parser.add_argument('--train-resizing', type=str, default='default')
    parser.add_argument('--val-resizing', type=str, default='default')
    # model parameters
    parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet50',
                        choices=utils.get_model_names(),
                        help='backbone architecture: ' +
                             ' | '.join(utils.get_model_names()) +
                             ' (default: resnet50)')
    parser.add_argument('--no-pool', action='store_true', help='no pool layer after the feature extractor.')
    parser.add_argument('--finetune', action='store_true', help='whether use 10x smaller lr for backbone')
    parser.add_argument('--freeze-bn', action='store_true', help='whether freeze all bn layers')
    parser.add_argument('--dropout-p', type=float, default=0.1, help='only activated when freeze-bn is True')
    # training parameters
    parser.add_argument('--n-support-domains', type=int, default=1,
                        help='Number of support domains sampled in each iteration')
    parser.add_argument('--n-query-domains', type=int, default=2,
                        help='Number of query domains in each iteration')
    parser.add_argument('--trade-off', type=float, default=1,
                        help='hyper parameter beta')
    parser.add_argument('-b', '--batch-size', default=36, type=int,
                        metavar='N',
                        help='mini-batch size (default: 36)')
    parser.add_argument('--lr', '--learning-rate', default=5e-4, type=float,
                        metavar='LR', help='initial learning rate', dest='lr')
    parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                        help='momentum')
    parser.add_argument('--wd', '--weight-decay', default=0.0005, type=float,
                        metavar='W', help='weight decay (default: 5e-4)')
    parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                        help='number of data loading workers (default: 4)')
    parser.add_argument('--epochs', default=20, type=int, metavar='N',
                        help='number of total epochs to run')
    parser.add_argument('-i', '--iters-per-epoch', default=500, type=int,
                        help='Number of iterations per epoch')
    parser.add_argument('--inner-iters', default=1, type=int,
                        help='Number of iterations in inner loop')
    parser.add_argument('-p', '--print-freq', default=100, type=int,
                        metavar='N', help='print frequency (default: 100)')
    parser.add_argument('--seed', default=None, type=int,
                        help='seed for initializing training. ')
    parser.add_argument("--log", type=str, default='mldg',
                        help="Where to save logs, checkpoints and debugging images.")
    parser.add_argument("--phase", type=str, default='train', choices=['train', 'test', 'analysis'],
                        help="When phase is 'test', only test the model."
                             "When phase is 'analysis', only analysis the model.")
    args = parser.parse_args()
    main(args)
