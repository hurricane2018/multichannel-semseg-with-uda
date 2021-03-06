from __future__ import division

import os

import six
import torch
import tqdm
from tensorboard_logger import configure, log_value
from torch.autograd import Variable
from torch.utils import data

from argmyparse import add_additional_params_to_args, get_da_mcd_training_parser
from datasets import ConcatDataset, get_dataset, check_src_tgt_ok
from joint_transforms import get_joint_transform
from loss import CrossEntropyLoss2d, get_prob_distance_criterion
from models.model_util import fix_batchnorm_when_training, get_optimizer, fix_dropout_when_training, \
    get_multitask_models
from transform import get_img_transform, \
    get_lbl_transform
from util import mkdir_if_not_exist, save_dic_to_json, check_if_done, save_checkpoint, adjust_learning_rate, \
    emphasize_str, get_class_weight_from_file

parser = get_da_mcd_training_parser()
args = parser.parse_args()
args = add_additional_params_to_args(args)

check_src_tgt_ok(args.src_dataset, args.tgt_dataset)

weight = get_class_weight_from_file(n_class=args.n_class, weight_filename=args.loss_weights_file,
                                    add_bg_loss=args.add_bg_loss)

if torch.cuda.is_available():
    weight = weight.cuda()

criterion = CrossEntropyLoss2d(weight)
criterion_d = get_prob_distance_criterion(args.d_loss)

resume_flg = True if args.resume else False
start_epoch = 0
if args.resume:
    print("=> loading checkpoint '{}'".format(args.resume))
    if not os.path.exists(args.resume):
        raise OSError("%s does not exist!" % args.resume)

    indir, infn = os.path.split(args.resume)

    old_savename = args.savename
    args.savename = infn.split("-")[0]
    print("savename is %s (original savename %s was overwritten)" % (args.savename, old_savename))

    checkpoint = torch.load(args.resume)
    start_epoch = checkpoint["epoch"]
    epochs = args.epochs
    # ---------- Replace Args!!! ----------- #
    args = checkpoint['args']
    # -------------------------------------- #
    args.epochs = epochs
    model_enc, model_dec = get_multitask_models(net_name=args.net, input_ch=args.input_ch,
                                                n_class=args.n_class,
                                                is_data_parallel=args.is_data_parallel,
                                                semseg_criterion=criterion, discrepancy_criterion=criterion_d)

    model_enc.load_state_dict(checkpoint['enc_state_dict'])
    model_dec.load_state_dict(checkpoint['dec_state_dict'])

    optimizer_enc = get_optimizer(model_enc.parameters(), lr=args.lr, momentum=args.momentum, opt=args.opt,
                                  weight_decay=args.weight_decay)
    optimizer_dec = get_optimizer(model_dec.parameters(), opt=args.opt,
                                  lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    optimizer_enc.load_state_dict(checkpoint["optimizer_enc"])
    optimizer_dec.load_state_dict(checkpoint["optimizer_dec"])
    print("=> loaded checkpoint '{}'".format(args.resume))

else:
    model_enc, model_dec = get_multitask_models(net_name=args.net, input_ch=args.input_ch,
                                                n_class=args.n_class, is_data_parallel=args.is_data_parallel,
                                                semseg_criterion=criterion, discrepancy_criterion=criterion_d)

    optimizer_enc = get_optimizer(model_enc.parameters(), lr=args.lr, momentum=args.momentum, opt=args.opt,
                                  weight_decay=args.weight_decay)
    optimizer_dec = get_optimizer(model_dec.parameters(), opt=args.opt,
                                  lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

mode = "%s-%s2%s-%s_%sch_MCDmultitask" % (
    args.src_dataset, args.src_split, args.tgt_dataset, args.tgt_split, args.input_ch)
if args.net in ["fcn", "psp"]:
    model_name = "%s-%s-%s-res%s" % (args.method, args.savename, args.net, args.res)
else:
    model_name = "%s-%s-%s" % (args.method, args.savename, args.net)

outdir = os.path.join(args.base_outdir, mode)

# Create Model Dir
pth_dir = os.path.join(outdir, "pth")
mkdir_if_not_exist(pth_dir)

# Create Model Dir and  Set TF-Logger
tflog_dir = os.path.join(outdir, "tflog", model_name)
mkdir_if_not_exist(tflog_dir)
configure(tflog_dir, flush_secs=5)

# Save param dic
if resume_flg:
    json_fn = os.path.join(outdir, "param-%s_resume.json" % model_name)
else:
    json_fn = os.path.join(outdir, "param-%s.json" % model_name)

check_if_done(json_fn)
save_dic_to_json(args.__dict__, json_fn)

train_img_shape = tuple([int(x) for x in args.train_img_shape])

use_crop = True if args.crop_size > 0 else False
joint_transform = get_joint_transform(crop_size=args.crop_size, rotate_angle=args.rotate_angle) if use_crop else None

img_transform = get_img_transform(img_shape=train_img_shape, normalize_way=args.normalize_way, use_crop=use_crop)

label_transform = get_lbl_transform(img_shape=train_img_shape, n_class=args.n_class, background_id=args.background_id,
                                    use_crop=use_crop)

src_dataset = get_dataset(dataset_name=args.src_dataset, split=args.src_split, img_transform=img_transform,
                          label_transform=label_transform, test=False, input_ch=args.input_ch)

tgt_dataset = get_dataset(dataset_name=args.tgt_dataset, split=args.tgt_split, img_transform=img_transform,
                          label_transform=label_transform, test=False, input_ch=args.input_ch)

train_loader = torch.utils.data.DataLoader(
    ConcatDataset(
        src_dataset,
        tgt_dataset
    ),
    batch_size=args.batch_size, shuffle=True,
    pin_memory=True)

weight = get_class_weight_from_file(n_class=args.n_class, weight_filename=args.loss_weights_file,
                                    add_bg_loss=args.add_bg_loss)

if torch.cuda.is_available():
    model_enc.cuda()
    model_dec.cuda()
    weight = weight.cuda()

model_enc.train()
model_dec.train()

w_depth = 0.1

if args.no_dropout:
    print("NO DROPOUT")
    fix_dropout_when_training(model_enc)
    fix_dropout_when_training(model_dec)

if args.fix_bn:
    print(emphasize_str("BN layers are NOT trained!"))
    fix_batchnorm_when_training(model_enc)
    fix_batchnorm_when_training(model_dec)

for epoch in range(start_epoch, args.epochs):
    c_loss_per_epoch = 0
    d_loss_per_epoch = 0

    src_semseg_loss_per_epoch = 0
    src_depth_loss_per_epoch = 0
    tgt_depth_loss_per_epoch = 0

    for ind, (source, target) in tqdm.tqdm(enumerate(train_loader)):
        src_imgs, src_gt_semseg = Variable(source[0]), Variable(source[1])
        tgt_imgs = Variable(target[0])

        if torch.cuda.is_available():
            src_imgs, src_gt_semseg, tgt_imgs = src_imgs.cuda(), src_gt_semseg.cuda(), tgt_imgs.cuda()

        src_rgbs = src_imgs[:, :3, :, :]
        src_depths = src_imgs[:, 3:, :, :]

        tgt_rgbs = tgt_imgs[:, :3, :, :]
        tgt_depths = tgt_imgs[:, 3:, :, :]

        # ---------- update generator and classifiers by source samples ---------- #
        optimizer_enc.zero_grad()
        optimizer_dec.zero_grad()

        src_fet = model_enc(src_rgbs)
        tgt_fet = model_enc(tgt_rgbs)

        src_semseg_loss, src_depth_loss = model_dec.get_loss(src_fet, src_gt_semseg, src_depths,
                                                             separately_returning=True)
        tgt_depth_loss = model_dec.get_depth_loss(tgt_fet, tgt_depths)

        src_semseg_loss_per_epoch += src_semseg_loss.data[0]
        src_depth_loss_per_epoch += src_depth_loss.data[0]
        tgt_depth_loss_per_epoch += tgt_depth_loss.data[0]

        loss = src_semseg_loss + src_depth_loss + tgt_depth_loss

        loss.backward()
        c_loss = loss.data[0]
        c_loss_per_epoch += c_loss

        optimizer_enc.step()
        optimizer_dec.step()

        # ---------- update for classifiers --------- #
        optimizer_enc.zero_grad()
        optimizer_dec.zero_grad()

        src_fet = model_enc(src_rgbs)
        src_pred_seg1, src_pred_seg2 = model_dec.semseg_forward(src_fet)

        src_semseg_loss, src_depth_loss = model_dec.get_loss(src_fet, src_gt_semseg, src_depths,
                                                             separately_returning=True)
        tgt_fet = model_enc(tgt_rgbs)
        tgt_depth_loss = model_dec.get_depth_loss(tgt_fet, tgt_depths)

        src_semseg_loss_per_epoch += src_semseg_loss.data[0]
        src_depth_loss_per_epoch += src_depth_loss.data[0]
        tgt_depth_loss_per_epoch += tgt_depth_loss.data[0]

        tgt_discrepancy = model_dec.get_cls_descrepancy(tgt_fet)
        loss = src_semseg_loss + src_depth_loss + tgt_depth_loss - tgt_discrepancy
        loss.backward()
        optimizer_dec.step()

        # ---------- update generator by discrepancy ---------- #
        for i in six.moves.range(args.num_k):
            optimizer_enc.zero_grad()
            tgt_fet = model_enc(tgt_rgbs)
            tgt_discrepancy = model_dec.get_cls_descrepancy(tgt_fet)
            loss = tgt_discrepancy * args.num_multiply_d_loss
            loss.backward()
            optimizer_enc.step()

        d_loss = loss.data[0] / args.num_k
        d_loss_per_epoch += d_loss
        if ind % 100 == 0:
            print("iter [%d] DLoss: %.6f CLoss: %.4f" % (ind, d_loss, c_loss))

        if ind > args.max_iter:
            break

    std_semseg, std_depth = model_dec.get_task_weights()
    print("std_semseg: %.4f, std_depth: %.4f" % (std_semseg, std_depth))

    print("Epoch [%d] DLoss: %.4f CLoss: %.4f" % (epoch, d_loss_per_epoch, c_loss_per_epoch))
    print("SrcSemsegLoss: %.4f, SrcDepthLoss: %.4f, SrcDepthLoss: %.4f" %
          (src_semseg_loss_per_epoch, src_depth_loss_per_epoch, tgt_depth_loss_per_epoch))

    log_value('c_loss', c_loss_per_epoch, epoch)
    log_value('d_loss', d_loss_per_epoch, epoch)
    log_value('src_semseg_loss', src_semseg_loss_per_epoch, epoch)
    log_value('src_depth_loss', src_depth_loss_per_epoch, epoch)
    log_value('tgt_depth_loss', tgt_depth_loss_per_epoch, epoch)
    log_value('lr', args.lr, epoch)

    log_value('std_semseg', std_semseg, epoch)
    log_value('std_depth', std_depth, epoch)

    if args.adjust_lr:
        args.lr = adjust_learning_rate(optimizer_enc, args.lr, args.weight_decay, epoch, args.epochs)
        args.lr = adjust_learning_rate(optimizer_dec, args.lr, args.weight_decay, epoch, args.epochs)

    checkpoint_fn = os.path.join(pth_dir, "%s-%s.pth.tar" % (model_name, epoch + 1))
    args.start_epoch = epoch + 1
    save_dic = {
        'epoch': epoch + 1,
        'args': args,
        'enc_state_dict': model_enc.state_dict(),
        'dec_state_dict': model_dec.state_dict(),
        'optimizer_enc': optimizer_enc.state_dict(),
        'optimizer_dec': optimizer_dec.state_dict(),
    }

    save_checkpoint(save_dic, is_best=False, filename=checkpoint_fn)
