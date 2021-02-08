import torch
import torch.nn as nn
import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.functional as F

import os
import shutil
import argparse
import numpy as np


import models
import torchvision
import torchvision.transforms as transforms
from utils import cal_param_size, cal_multi_adds


from bisect import bisect_right
import time
import math

parser = argparse.ArgumentParser(description='PyTorch CIFAR Training')
parser.add_argument('--data', default='./data/', type=str, help='Dataset directory')
parser.add_argument('--arch', default='resnet32', type=str, help='network architecture')
parser.add_argument('--init-lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--lr-type', default='SGDR', type=str, help='learning rate strategy')
parser.add_argument('--milestones', default=[150, 225], type=list, help='milestones for lr-multistep')
parser.add_argument('--sgdr-t', default=300, type=int, dest='sgdr_t',help='SGDR T_0')
parser.add_argument('--epochs', type=int, default=300, help='number of epochs to train')
parser.add_argument('--batch-size', type=int, default=128, help='batch size')
parser.add_argument('--num-workers', type=int, default=8, help='number of workers')
parser.add_argument('--gpu-id', type=str, default='0')
parser.add_argument('--manual_seed', type=int, default=0)
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--evaluate', '-e', action='store_true', help='evaluate model')

# global hyperparameter set
args = parser.parse_args()
os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu_id

log_txt = 'result/'+ str(os.path.basename(__file__).split('.')[0]) + args.arch +'_'+str(args.manual_seed)+'.txt'


np.random.seed(args.manual_seed)
torch.manual_seed(args.manual_seed)
torch.cuda.manual_seed_all(args.manual_seed)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
best_acc = 0.  # best test accuracy
start_epoch = 0  # start from epoch 0 or last checkpoint epoch
# -----------------------------------------------------------------------------------------
# dataset
num_classes = 100
trainset = torchvision.datasets.CIFAR100(root=args.data, train=True, download=True,
                                         transform=transforms.Compose([
                                             transforms.RandomCrop(32, padding=4),
                                             transforms.RandomHorizontalFlip(),
                                             transforms.ToTensor(),
                                             transforms.Normalize([0.5071, 0.4867, 0.4408],
                                                                  [0.2675, 0.2565, 0.2761])
                                         ]))

testset = torchvision.datasets.CIFAR100(root=args.data, train=False, download=True,
                                        transform=transforms.Compose([
                                            transforms.ToTensor(),
                                            transforms.Normalize([0.5071, 0.4867, 0.4408],
                                                                 [0.2675, 0.2565, 0.2761]),
                                        ]))
trainloader = torch.utils.data.DataLoader(trainset, batch_size=128, shuffle=True,
                                      pin_memory=(torch.cuda.is_available()))

testloader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False,
                                     pin_memory=(torch.cuda.is_available()))
# --------------------------------------------------------------------------------------------

# Model
print('==> Building model..')
model = getattr(models, args.arch)
net = model(num_classes=num_classes)
print('Params: %.2fM, Multi-adds: %.2fG'
      % (cal_param_size(net)/1e6, cal_multi_adds(net, (2, 3, 32, 32))/1e9))
del(net)

net = model(num_classes=num_classes).cuda()
net = torch.nn.DataParallel(net)
cudnn.benchmark = True


def adjust_lr(optimizer, epoch, eta_max=0.1, eta_min=0.):
    cur_lr = 0.
    if args.lr_type == 'SGDR':
        i = int(math.log2(epoch / args.sgdr_t + 1))
        T_cur = epoch - args.sgdr_t * (2 ** (i) - 1)
        T_i = (args.sgdr_t * 2 ** i)

        cur_lr = eta_min + 0.5 * (eta_max - eta_min) * (1 + np.cos(np.pi * T_cur / T_i))

    elif args.lr_type == 'multistep':
        cur_lr = args.init_lr * 0.1 ** bisect_right(args.milestones, epoch)

    for param_group in optimizer.param_groups:
        param_group['lr'] = cur_lr
    return cur_lr


# Training
def train(epoch, criterion_list, optimizer):
    train_loss = 0.

    correct = 0
    total = 0

    lr = adjust_lr(optimizer, epoch)
    start_time = time.time()
    criterion_cls = criterion_list[0]

    net.train()
    for batch_idx, (input, target) in enumerate(trainloader):
        input = input.float()
        input = input.cuda()
        target = target.cuda()
        optimizer.zero_grad()
        logit = net(input)

        loss = criterion_cls(logit, target)
        loss.backward()
        optimizer.step()

        train_loss += loss.item() / len(trainloader)

        _, predicted = logit.max(1)
        correct += predicted.eq(target).sum().item()
        total += target.size(0)

    acc = correct / total
    with open(log_txt, 'a+') as f:
        f.write('Epoch:{0}\t lr:{1:.3f}\t duration:{2:.3f}'
                '\n train_loss:{3:.5f}'
                '\n accuracy: {4} \n'
                .format(epoch, lr, time.time() - start_time,
                        train_loss, str(acc)))

def test(epoch, criterion_cls):
    net.eval()
    global best_acc
    test_loss_cls = 0.

    correct = 0
    total = 0

    with torch.no_grad():
        for batch_idx, (inputs, target) in enumerate(testloader):
            inputs, target = inputs.cuda(), target.cuda()
            logit= net(inputs)

            loss_cls = 0.
            loss_cls = loss_cls + criterion_cls(logit, target)

            test_loss_cls += loss_cls.item()/ len(testloader)
            _, predicted = logit.max(1)
            correct += predicted.eq(target).sum().item()
            total += target.size(0)

        acc = correct / total

        with open(log_txt, 'a+') as f:
            f.write('test epoch:{0}\t test_loss_cls:{1:.5f}\t accuracy:{2}\n'
                    .format(epoch, test_loss_cls, str(acc)))
        print('test epoch:{0}\t accuracy:{1}\n'.format(epoch, str(acc)))

    return acc


def main():
    global best_acc
    global start_epoch
    criterion_cls = nn.CrossEntropyLoss()

    if args.evaluate:
        checkpoint = torch.load('./checkpoint/' + model.__name__ + '_best.pth.tar',
                                map_location=torch.device('cpu'))
        net.load_state_dict(checkpoint['net'])
        best_acc = checkpoint['acc']
        start_epoch = checkpoint['epoch'] + 1
        test(start_epoch, criterion_cls)
    else:
        trainable_list = nn.ModuleList([])
        trainable_list.append(net)

        optimizer = optim.SGD(trainable_list.parameters(), lr=0.1, momentum=0.9, weight_decay=5e-4, nesterov=True)

        criterion_list = nn.ModuleList([])
        criterion_list.append(criterion_cls)  # classification loss
        criterion_list.cuda()

        if args.resume:
            checkpoint = torch.load('./checkpoint/' + model.__name__ + '.pth.tar', map_location=torch.device('cpu'))
            net.load_state_dict(checkpoint['net'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            best_acc = checkpoint['acc']
            start_epoch = checkpoint['epoch']

        for epoch in range(start_epoch, args.epochs):
            train(epoch, criterion_list, optimizer)
            acc = test(epoch, criterion_cls)

            state = {
                'net': net.state_dict(),
                'acc': acc,
                'epoch': epoch,
                'optimizer': optimizer.state_dict()
            }
            if not os.path.isdir('checkpoint'):
                os.mkdir('checkpoint')
            torch.save(state, './checkpoint/' + str(model.__name__) + '.pth.tar')

            is_best = False
            if best_acc < acc:
                best_acc = acc
                is_best = True

            if is_best:
                shutil.copyfile('./checkpoint/' + str(model.__name__) + '.pth.tar',
                                './checkpoint/' + str(model.__name__) + '_best.pth.tar')


if __name__ == '__main__':
    main()
