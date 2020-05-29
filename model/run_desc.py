import matplotlib.pyplot as plt
# * For visualizing thingy
import numpy as np
import torch
import torch.nn.functional as F

from misc.utils import center_pad_to_shape, cropping_center
from .utils import crop_to_shape, dice_loss, mse_loss, msge_loss, xentropy_loss

####
def train_step(batch_data, run_info):
    # TODO: synchronize the attach protocol
    # use 'ema' to add for EMA calculation, must be scalar!
    result_dict = {'EMA' : {}} 
    track_value = lambda name, value: result_dict['EMA'].update({name: value})

    ####
    imgs = batch_data['img']
    true_np = batch_data['np_map']
    true_hv = batch_data['hv_map']
   
    imgs = imgs.to('cuda').type(torch.float32) # to NCHW
    imgs = imgs.permute(0, 3, 1, 2).contiguous()

    # HWC
    true_np = torch.squeeze(true_np).to('cuda').type(torch.int64)
    true_hv = torch.squeeze(true_hv).to('cuda').type(torch.float32)

    # true_tp = batch_label[...,2:]

    ####
    model     = run_info['net']['desc']
    optimizer = run_info['net']['optimizer']
    ####
    model.train() 
    model.zero_grad() # not rnn so not accumulate

    output_dict = model(imgs)
    output_dict = {k : v.permute(0, 2, 3 ,1).contiguous() for k, v in output_dict.items()}

    pred_np = output_dict['np'] # should be logit value, not softmax output
    pred_hv = output_dict['hv']

    prob_np = F.softmax(pred_np, dim=-1)
    ####
    loss = 0

    # TODO: adding loss weighting mechanism
    # * For Nulcei vs Background Segmentation 
    # NP branch
    true_np_onehot = (F.one_hot(true_np, num_classes=2)).float()
    term_loss = xentropy_loss(prob_np, true_np_onehot, reduction='mean')
    track_value('np_xentropy_loss', term_loss.cpu().item())
    loss += term_loss

    term_loss = dice_loss(prob_np[...,0], true_np_onehot[...,0]) \
              + dice_loss(prob_np[...,1], true_np_onehot[...,1])
    track_value('np_dice_loss', term_loss.cpu().item())
    loss += term_loss
    
    # HV branch
    term_loss = mse_loss(pred_hv, true_hv)
    track_value('hv_mse_loss', term_loss.cpu().item())
    loss += 2 * term_loss

    term_loss = msge_loss(pred_hv, true_hv, true_np)
    track_value('hv_msge_loss', term_loss.cpu().item())
    loss += term_loss

    track_value('overall_loss', loss.cpu().item())
    # * gradient update

    # torch.set_printoptions(precision=10)
    loss.backward()
    optimizer.step()
    ####

    # pick 2 random sample from the batch for visualization
    sample_indices = torch.randint(0, true_np.shape[0], (2,))

    imgs = (imgs[sample_indices]).byte() # to uint8
    imgs = imgs.permute(0, 2, 3, 1).cpu().numpy()

    pred_hv = pred_hv.detach()[sample_indices].cpu().numpy()
    true_hv = true_hv[sample_indices].cpu().numpy()

    prob_np = prob_np.detach()[...,1:][sample_indices].cpu().numpy()
    true_np = true_np.float()[...,None][sample_indices].cpu().numpy()

    # * Its up to user to define the protocol to process the raw output per step!
    result_dict['raw'] = { # protocol for contents exchange within `raw`
        'img': imgs,
        'np' : (true_np, prob_np),
        'hv' : (true_hv, pred_hv)
    }
    return result_dict

####
def valid_step(batch_data, run_info):

    ####
    imgs = batch_data['img']
    true_np = batch_data['np_map']
    true_hv = batch_data['hv_map']
   
    imgs = imgs.to('cuda').type(torch.float32) # to NCHW
    imgs = imgs.permute(0, 3, 1, 2)

    # HWC
    true_np = torch.squeeze(true_np).to('cuda').type(torch.int64)
    true_hv = torch.squeeze(true_hv).to('cuda').type(torch.float32)

    ####
    model = run_info['net']['desc']
    model.eval() # infer mode

    # -----------------------------------------------------------
    with torch.no_grad(): # dont compute gradient
        output_dict = model(imgs) # forward
    output_dict = {k : v.permute(0, 2, 3 ,1) for k, v in output_dict.items()}

    pred_np = output_dict['np'] # should be logit value, not softmax output
    pred_hv = output_dict['hv']
    prob_np = F.softmax(pred_np, dim=-1)[...,1]

    # * Its up to user to define the protocol to process the raw output per step!
    result_dict = { # protocol for contents exchange within `raw`
        'raw': {
            'true_np' : true_np.cpu().numpy(),
            'true_hv' : true_hv.cpu().numpy(),
            'prob_np' : prob_np.cpu().numpy(),
            'pred_hv' : pred_hv.cpu().numpy()
        }
    }
    return result_dict

####
def viz_train_step_output(raw_data):
    """
    `raw_data` will be implicitly provided in the similar format as the 
    return dict from train/valid step, but may have been accumulated across N running step
    """

    imgs = raw_data['img']
    true_np, pred_np = raw_data['np']
    true_hv, pred_hv = raw_data['hv']

    aligned_shape = [list(imgs.shape), list(true_np.shape), list(pred_np.shape)]
    aligned_shape = np.min(np.array(aligned_shape), axis=0)[1:3]

    cmap = plt.get_cmap('jet')

    def colorize(ch, vmin, vmax):
        """
        Will clamp value value outside the provided range to vmax and vmin
        """
        ch = np.squeeze(ch.astype('float32'))
        ch[ch > vmax] = vmax # clamp value
        ch[ch < vmin] = vmin
        ch = (ch - vmin) / (vmax - vmin + 1.0e-16)
        # take RGB from RGBA heat map
        ch_cmap = (cmap(ch)[..., :3] * 255).astype('uint8')
        # ch_cmap = center_pad_to_shape(ch_cmap, aligned_shape)
        return ch_cmap

    viz_list = []
    for idx in range(imgs.shape[0]):
        # img = center_pad_to_shape(imgs[idx], aligned_shape)
        img = cropping_center(imgs[idx], aligned_shape)

        true_viz_list = [img]
        # cmap may randomly fails if of other types
        true_viz_list.append(colorize(true_np[idx], 0, 1))
        true_viz_list.append(colorize(true_hv[idx][..., 0], -1, 1))
        true_viz_list.append(colorize(true_hv[idx][..., 1], -1, 1))
        true_viz_list = np.concatenate(true_viz_list, axis=1)

        pred_viz_list = [img]
        # cmap may randomly fails if of other types
        pred_viz_list.append(colorize(pred_np[idx], 0, 1))
        pred_viz_list.append(colorize(pred_hv[idx][..., 0], -1, 1))
        pred_viz_list.append(colorize(pred_hv[idx][..., 1], -1, 1))
        pred_viz_list = np.concatenate(pred_viz_list, axis=1)

        viz_list.append(
            np.concatenate([true_viz_list, pred_viz_list], axis=0)
        )
    viz_list = np.concatenate(viz_list, axis=0)
    # plt.imshow(viz_list)
    # plt.savefig('dump.png', dpi=600)
    return viz_list

####
def proc_valid_step_output(raw_data):
    # TODO: add auto populate from main state track list
    track_dict = {'scalar': {}}
    def track_value(name, value, vtype): return track_dict[vtype].update(
        {name: value})

    # ! factor this out
    def _dice(true, pred, label):
        true = np.array(true == label, np.int32)
        pred = np.array(pred == label, np.int32)
        inter = (pred * true).sum()
        total = (pred + true).sum()
        return 2 * inter / (total + 1.0e-8)

    pred_np = np.array(raw_data['prob_np'])
    true_np = np.array(raw_data['true_np'])
    nr_pixels = np.size(true_np)
    # * NP segmentation statistic
    pred_np[pred_np > 0.5] = 1.0
    pred_np[pred_np <= 0.5] = 0.0

    # TODO: something sketchy here
    acc_np = (pred_np == true_np).sum() / nr_pixels
    dice_np = _dice(true_np, pred_np, 1)
    track_value('np_acc', acc_np, 'scalar')
    track_value('np_dice', dice_np, 'scalar')

    # * HV regression statistic
    pred_hv = np.array(raw_data['pred_hv'])
    true_hv = np.array(raw_data['true_hv'])
    error = pred_hv - true_hv
    mse = np.sum(error * error) / nr_pixels
    track_value('hv_mse', mse, 'scalar')

    # idx = np.random.randint(0, true_np.shape[0])
    # plt.subplot(2,3,1)
    # plt.imshow(true_np[idx], cmap='jet')
    # plt.subplot(2,3,2)
    # plt.imshow(true_hv[idx,...,0], cmap='jet')
    # plt.subplot(2,3,3)
    # plt.imshow(true_hv[idx,...,1], cmap='jet')
    # plt.subplot(2,3,4)
    # plt.imshow(pred_np[idx], cmap='jet')
    # plt.subplot(2,3,5)
    # plt.imshow(pred_hv[idx,...,0], cmap='jet')
    # plt.subplot(2,3,6)
    # plt.imshow(pred_hv[idx,...,1], cmap='jet')
    # plt.savefig('dumpx.png', dpi=600)
    # plt.close()

    # idx = np.random.randint(0, true_np.shape[0])
    # plt.subplot(2,3,1)
    # plt.imshow(true_np[idx], cmap='jet')
    # plt.subplot(2,3,2)
    # plt.imshow(true_hv[idx,...,0], cmap='jet')
    # plt.subplot(2,3,3)
    # plt.imshow(true_hv[idx,...,1], cmap='jet')
    # plt.subplot(2,3,4)
    # plt.imshow(pred_np[idx], cmap='jet')
    # plt.subplot(2,3,5)
    # plt.imshow(pred_hv[idx,...,0], cmap='jet')
    # plt.subplot(2,3,6)
    # plt.imshow(pred_hv[idx,...,1], cmap='jet')
    # plt.savefig('dumpy.png', dpi=600)
    # plt.close()
    return track_dict