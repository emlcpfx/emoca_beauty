import os, sys
import torch
import torchvision
import torch.nn.functional as F
from pytorch_lightning import LightningModule
from layers.losses.EmoNetLoss import EmoNetLoss
import numpy as np
# from time import time
from skimage.io import imread
import cv2
from pathlib import Path

# add DECA's repo
sys.path += [str(Path(__file__).parent.parent.parent.absolute() / 'DECA-training')]
from lib.utils.renderer import SRenderY
from lib.models.encoders import ResnetEncoder
from lib.models.decoders import Generator
from lib.models.FLAME import FLAME, FLAMETex
from lib.utils import lossfunc, util
# from . import datasets
from lib.datasets.datasets import VoxelDataset, TestData
import lib.utils.util as util
import lib.utils.lossfunc as lossfunc


torch.backends.cudnn.benchmark = True
from enum import Enum


class DecaMode(Enum):
    COARSE = 1
    DETAIL = 2


class DecaModule(LightningModule):

    def __init__(self, model_params, learning_params, inout_params):
        super().__init__()
        self.learning_params = learning_params
        self.inout_params = inout_params
        self.deca = DECA(config=model_params)
        self.mode = DecaMode[str(model_params.mode).upper()]

        if 'emonet_reg' in self.deca.config.keys():
            self.emonet_loss = EmoNetLoss(self.device)
        else:
            self.emonet_loss = None

    def reconfigure(self, model_params):
        if self.mode == DecaMode.DETAIL and model_params.mode != DecaMode.DETAIL:
            raise RuntimeError("You're switching the DECA mode from DETAIL to COARSE. Is this really what you want?!")
        self.deca._reconfigure(model_params)
        self.mode = DecaMode[str(model_params.mode).upper()]
        print(f"DECA MODE RECONFIGURED TO: {self.mode}")

    def _move_extra_params_to_correct_device(self):
        if self.deca.uv_face_eye_mask.device != self.device:
            self.deca.uv_face_eye_mask = self.deca.uv_face_eye_mask.to(self.device)
        if self.deca.fixed_uv_dis.device != self.device:
            self.deca.fixed_uv_dis = self.deca.fixed_uv_dis.to(self.device)
        if self.emonet_loss is not None:
            self.emonet_loss.to(device=self.device)

    def train(self, mode: bool = True):
        super().train(mode)
        if mode:
            if self.mode == DecaMode.COARSE:
                self.deca.E_flame.train()
                self.deca.E_detail.eval()
                self.deca.D_detail.eval()
            if self.mode == DecaMode.DETAIL:
                if self.deca.config.train_coarse:
                    self.deca.E_flame.train()
                else:
                    self.deca.E_flame.eval()
                self.deca.E_detail.train()
                self.deca.D_detail.train()
            if self.emonet_loss is not None:
                self.emonet_loss.eval()
        return self

    def to(self, *args, **kwargs):
        super().to(*args, **kwargs)
        # if 'device' in kwargs.keys():
        self._move_extra_params_to_correct_device()
        return self

    def cuda(self, device=None):
        super().cuda(device)
        self._move_extra_params_to_correct_device()
        return self

    def cpu(self):
        super().cpu()
        self._move_extra_params_to_correct_device()
        return self

    # def forward(self, image):
    #     codedict = self.deca.encode(image)
    #     opdict, visdict = self.deca.decode(codedict)
    #     opdict = dict_tensor2npy(opdict)


    def _encode(self, batch, training=True) -> dict:
        codedict = {}
        original_batch_size = batch['image'].shape[0]

        # [B, K, 3, size, size] ==> [BxK, 3, size, size]
        images = batch['image']
        images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])

        if 'landmark' in batch.keys():
            lmk = batch['landmark']
            lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
        if 'mask' in batch.keys():
            masks = batch['mask']
            masks = masks.view(-1, images.shape[-2], images.shape[-1])

        if self.mode == DecaMode.DETAIL:
            with torch.no_grad():
                parameters = self.deca.E_flame(images)
        elif self.mode == DecaMode.COARSE:
            parameters = self.deca.E_flame(images)
        else:
            raise ValueError(f"Invalid DECA Mode {self.mode}")

        code_list = self.deca.decompose_code(parameters)
        shapecode, texcode, expcode, posecode, cam, lightcode = code_list

        # #TODO: figure out if we want to keep this code block:
        # if self.config.model.jaw_type == 'euler':
        #     # if use euler angle
        #     euler_jaw_pose = posecode[:, 3:].clone()  # x for yaw (open mouth), y for pitch (left ang right), z for roll
        #     # euler_jaw_pose[:,0] = 0.
        #     # euler_jaw_pose[:,1] = 0.
        #     # euler_jaw_pose[:,2] = 30.
        #     posecode[:, 3:] = batch_euler2axis(euler_jaw_pose)

        if training:
            if self.mode == DecaMode.COARSE:
                ### shape constraints
                if self.deca.config.shape_constrain_type == 'same':
                    # reshape shapecode => [B, K, n_shape]
                    shapecode_idK = shapecode.view(self.batch_size, self.deca.K, -1)
                    # get mean id
                    shapecode_mean = torch.mean(shapecode_idK, dim=[1])
                    shapecode_new = shapecode_mean[:, None, :].repeat(1, self.deca.K, 1)
                    shapecode = shapecode_new.view(-1, self.deca.config.model.n_shape)
                elif self.deca.config.shape_constrain_type == 'exchange':
                    '''
                    make sure s0, s1 is something to make shape close
                    the difference from ||so - s1|| is 
                    the later encourage s0, s1 is cloase in l2 space, but not really ensure shape will be close
                    '''
                    # new_order = np.array([np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(self.deca.config.batch_size_train)])
                    new_order = np.array([np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(original_batch_size)])
                    new_order = new_order.flatten()
                    shapecode_new = shapecode[new_order]
                    # import ipdb; ipdb.set_trace()
                    ## append new shape code data
                    shapecode = torch.cat([shapecode, shapecode_new], dim=0)
                    texcode = torch.cat([texcode, texcode], dim=0)
                    expcode = torch.cat([expcode, expcode], dim=0)
                    posecode = torch.cat([posecode, posecode], dim=0)
                    cam = torch.cat([cam, cam], dim=0)
                    lightcode = torch.cat([lightcode, lightcode], dim=0)
                    ## append gt
                    images = torch.cat([images, images],
                                       dim=0)  # images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
                    lmk = torch.cat([lmk, lmk], dim=0)  # lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
                    masks = torch.cat([masks, masks], dim=0)
                # import ipdb; ipdb.set_trace()

        # -- detail
        if self.mode == DecaMode.DETAIL:
            detailcode = self.deca.E_detail(images)

            if training:
                if self.deca.config.detail_constrain_type == 'exchange':
                    '''
                    make sure s0, s1 is something to make shape close
                    the difference from ||so - s1|| is 
                    the later encourage s0, s1 is cloase in l2 space, but not really ensure shape will be close
                    '''
                    # new_order = np.array(
                    #     [np.random.permutation(self.deca.config.K) + i * self.deca.config.K for i in range(self.deca.config.effective_batch_size)])
                    new_order = np.array(
                        [np.random.permutation(self.deca.config.train_K) + i * self.deca.config.train_K for i in range(original_batch_size)])
                    new_order = new_order.flatten()
                    detailcode_new = detailcode[new_order]
                    # import ipdb; ipdb.set_trace()
                    detailcode = torch.cat([detailcode, detailcode_new], dim=0)
                    ## append new shape code data
                    shapecode = torch.cat([shapecode, shapecode], dim=0)
                    texcode = torch.cat([texcode, texcode], dim=0)
                    expcode = torch.cat([expcode, expcode], dim=0)
                    posecode = torch.cat([posecode, posecode], dim=0)
                    cam = torch.cat([cam, cam], dim=0)
                    lightcode = torch.cat([lightcode, lightcode], dim=0)
                    ## append gt
                    images = torch.cat([images, images],
                                       dim=0)  # images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
                    lmk = torch.cat([lmk, lmk], dim=0)  # lmk = lmk.view(-1, lmk.shape[-2], lmk.shape[-1])
                    masks = torch.cat([masks, masks], dim=0)

        codedict['shapecode'] = shapecode
        codedict['texcode'] = texcode
        codedict['expcode'] = expcode
        codedict['posecode'] = posecode
        codedict['cam'] = cam
        codedict['lightcode'] = lightcode
        if self.mode == DecaMode.DETAIL:
            codedict['detailcode'] = detailcode
        codedict['images'] = images
        if 'mask' in batch.keys():
            codedict['masks'] = masks
        if 'landmark' in batch.keys():
            codedict['lmk'] = lmk
        return codedict


    def _decode(self, codedict, training=True) -> dict:
        shapecode = codedict['shapecode']
        expcode = codedict['expcode']
        posecode = codedict['posecode']
        texcode = codedict['texcode']
        cam = codedict['cam']
        lightcode = codedict['lightcode']
        images = codedict['images']
        masks = codedict['masks']

        effective_batch_size = images.shape[0]  # this is the current batch size after all training augmentations modifications

        # FLAME - world space
        verts, landmarks2d, landmarks3d = self.deca.flame(shape_params=shapecode, expression_params=expcode,
                                                          pose_params=posecode)
        # world to camera
        trans_verts = util.batch_orth_proj(verts, cam)
        predicted_landmarks = util.batch_orth_proj(landmarks2d, cam)[:, :, :2]
        # camera to image space
        trans_verts[:, :, 1:] = -trans_verts[:, :, 1:]
        predicted_landmarks[:, :, 1:] = - predicted_landmarks[:, :, 1:]

        albedo = self.deca.flametex(texcode)

        # ------ rendering
        ops = self.deca.render(verts, trans_verts, albedo, lightcode)
        # mask
        mask_face_eye = F.grid_sample(self.deca.uv_face_eye_mask.expand(effective_batch_size, -1, -1, -1),
                                      ops['grid'].detach(),
                                      align_corners=False)
        # images
        predicted_images = ops['images'] * mask_face_eye * ops['alpha_images']

        if self.deca.config.useSeg:
            masks = masks[:, None, :, :]
        else:
            masks = mask_face_eye * ops['alpha_images']

        if self.mode == DecaMode.DETAIL:
            detailcode = codedict['detailcode']
            uv_z = self.deca.D_detail(torch.cat([posecode[:, 3:], expcode, detailcode], dim=1))
            # render detail
            uv_detail_normals, uv_coarse_vertices = self.deca.displacement2normal(uv_z, verts, ops['normals'])
            uv_shading = self.deca.render.add_SHlight(uv_detail_normals, lightcode.detach())
            uv_texture = albedo.detach() * uv_shading
            predicted_detailed_image = F.grid_sample(uv_texture, ops['grid'].detach(), align_corners=False)

            # --- extract texture
            uv_pverts = self.deca.render.world2uv(trans_verts).detach()
            uv_gt = F.grid_sample(torch.cat([images, masks], dim=1), uv_pverts.permute(0, 2, 3, 1)[:, :, :, :2],
                                  mode='bilinear')
            uv_texture_gt = uv_gt[:, :3, :, :].detach()
            uv_mask_gt = uv_gt[:, 3:, :, :].detach()
            # self-occlusion
            normals = util.vertex_normals(trans_verts, self.deca.render.faces.expand(effective_batch_size, -1, -1))
            uv_pnorm = self.deca.render.world2uv(normals)

            uv_mask = (uv_pnorm[:, -1, :, :] < -0.05).float().detach()
            uv_mask = uv_mask[:, None, :, :]
            ## combine masks
            uv_vis_mask = uv_mask_gt * uv_mask * self.deca.uv_face_eye_mask
        else:
            uv_detail_normals = None
            predicted_detailed_image = None

        # populate the value dict for metric computation/visualization
        codedict['predicted_images'] = predicted_images
        codedict['predicted_detailed_image'] = predicted_detailed_image
        codedict['verts'] = verts
        codedict['albedo'] = albedo
        codedict['mask_face_eye'] = mask_face_eye
        codedict['landmarks2d'] = landmarks2d
        codedict['landmarks3d'] = landmarks3d
        codedict['predicted_landmarks'] = predicted_landmarks
        codedict['trans_verts'] = trans_verts
        codedict['ops'] = ops
        codedict['masks'] = masks

        if self.mode == DecaMode.DETAIL:
            codedict['uv_texture_gt'] = uv_texture_gt
            codedict['uv_texture'] = uv_texture
            codedict['uv_detail_normals'] = uv_detail_normals
            codedict['uv_z'] = uv_z
            codedict['uv_shading'] = uv_shading
            codedict['uv_vis_mask'] = uv_vis_mask

        return codedict

    def _compute_emotion_loss(self, images, predicted_images, loss_dict, metric_dict, prefix):
        emo_feat_loss_1, emo_feat_loss_2, valence_loss, arousal_loss, expression_loss = \
            self.emonet_loss.compute_loss(images, predicted_images)
        if self.deca.config.use_emonet_loss:
            d = loss_dict
        else:
            d = metric_dict
        d[prefix + '_emo_feat_1_L1'] = emo_feat_loss_1 * self.deca.config.emonet_reg
        d[prefix + '_emo_feat_2_L1'] = emo_feat_loss_2 * self.deca.config.emonet_reg
        d[prefix + '_valence_L1'] = valence_loss * self.deca.config.emonet_reg
        d[prefix + '_arousal_L1'] = arousal_loss * self.deca.config.emonet_reg
        d[prefix + '_expression_KL'] = expression_loss * self.deca.config.emonet_reg
        d[prefix + '_emotion_combined'] = (emo_feat_loss_1 + emo_feat_loss_2 + valence_loss + arousal_loss + expression_loss) * self.deca.config.emonet_reg

    def _compute_loss(self, codedict, training=True) -> (dict, dict):
        #### ----------------------- Losses
        losses = {}
        metrics = {}

        predicted_landmarks = codedict["predicted_landmarks"]
        lmk = codedict["lmk"]
        masks = codedict["masks"]
        predicted_images = codedict["predicted_images"]
        images = codedict["images"]
        lightcode = codedict["lightcode"]
        albedo = codedict["albedo"]
        mask_face_eye = codedict["mask_face_eye"]
        shapecode = codedict["shapecode"]
        expcode = codedict["expcode"]
        texcode = codedict["texcode"]
        ops = codedict["ops"]
        if self.mode == DecaMode.DETAIL:
            uv_texture = codedict["uv_texture"]
            uv_texture_gt = codedict["uv_texture_gt"]


        ## COARSE loss only
        if self.mode == DecaMode.COARSE or (self.mode == DecaMode.DETAIL and self.deca.config.train_coarse):

            # landmark losses (only useful if coarse model is being trained
            if self.deca.config.useWlmk:
                losses['landmark'] = lossfunc.weighted_landmark_loss(predicted_landmarks,
                                                                          lmk) * self.deca.config.lmk_weight
            else:
                losses['landmark'] = lossfunc.landmark_loss(predicted_landmarks, lmk) * self.deca.config.lmk_weight
            # losses['eye_distance'] = lossfunc.eyed_loss(predicted_landmarks, lmk) * self.deca.config.lmk_weight * 2
            losses['eye_distance'] = lossfunc.eyed_loss(predicted_landmarks, lmk) * self.deca.config.eyed
            losses['lip_distance'] = lossfunc.eyed_loss(predicted_landmarks, lmk) * self.deca.config.lipd

            # photometric loss
            losses['photometric_texture'] = (masks * (predicted_images - images).abs()).mean() * self.deca.config.photow

            if self.deca.config.idw > 1e-3:
                shading_images = self.deca.render.add_SHlight(ops['normal_images'], lightcode.detach())
                albedo_images = F.grid_sample(albedo.detach(), ops['grid'], align_corners=False)
                overlay = albedo_images * shading_images * mask_face_eye + images * (1 - mask_face_eye)
                losses['identity'] = self.deca.id_loss(overlay, images) * self.deca.config.idw

            losses['shape_reg'] = (torch.sum(shapecode ** 2) / 2) * self.deca.config.shape_reg
            losses['expression_reg'] = (torch.sum(expcode ** 2) / 2) * self.deca.config.exp_reg
            losses['tex_reg'] = (torch.sum(texcode ** 2) / 2) * self.deca.config.tex_reg
            losses['light_reg'] = ((torch.mean(lightcode, dim=2)[:, :,
                                    None] - lightcode) ** 2).mean() * self.deca.config.light_reg

            if self.emonet_loss is not None:
                self._compute_emotion_loss(images, predicted_images, losses, metrics, "coarse")

        ## DETAIL loss only
        if self.mode == DecaMode.DETAIL:
            predicted_detailed_image = codedict["predicted_detailed_image"]
            uv_z = codedict["uv_z"]
            uv_shading = codedict["uv_shading"]
            uv_vis_mask = codedict["uv_vis_mask"]

            metrics['photometric_detailed_texture'] = (masks * (
                    predicted_detailed_image - images).abs()).mean() * self.deca.config.photow

            if self.emonet_loss is not None:
                self._compute_emotion_loss(images, predicted_detailed_image, losses, metrics, "detail")

            for pi in range(3):  # self.deca.face_attr_mask.shape[0]):
                # if pi==0:
                new_size = 256
                # else:
                #     new_size = 128
                # if self.deca.config.uv_size != 256:
                #     new_size = 128
                uv_texture_patch = F.interpolate(
                    uv_texture[:, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                    self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]],
                    [new_size, new_size], mode='bilinear')
                uv_texture_gt_patch = F.interpolate(
                    uv_texture_gt[:, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                    self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]], [new_size, new_size],
                    mode='bilinear')
                uv_vis_mask_patch = F.interpolate(
                    uv_vis_mask[:, :, self.deca.face_attr_mask[pi][2]:self.deca.face_attr_mask[pi][3],
                    self.deca.face_attr_mask[pi][0]:self.deca.face_attr_mask[pi][1]],
                    [new_size, new_size], mode='bilinear')

                losses['detail_l1_{}'.format(pi)] = (
                                                            uv_texture_patch * uv_vis_mask_patch - uv_texture_gt_patch * uv_vis_mask_patch).abs().mean() * \
                                                    self.deca.config.sfsw[pi]
                losses['detail_mrf_{}'.format(pi)] = self.deca.perceptual_loss(uv_texture_patch * uv_vis_mask_patch,
                                                                               uv_texture_gt_patch * uv_vis_mask_patch) * \
                                                     self.deca.config.sfsw[pi] * self.deca.config.mrfwr

                # if pi == 2:
                #     uv_texture_gt_patch_ = uv_texture_gt_patch
                #     uv_texture_patch_ = uv_texture_patch
                #     uv_vis_mask_patch_ = uv_vis_mask_patch

            losses['z_reg'] = torch.mean(uv_z.abs()) * self.deca.config.zregw
            losses['z_diff'] = lossfunc.shading_smooth_loss(uv_shading) * self.deca.config.zdiffw
            nonvis_mask = (1 - util.binary_erosion(uv_vis_mask))
            losses['z_sym'] = (nonvis_mask * (
                        uv_z - torch.flip(uv_z, [-1]).detach()).abs()).sum() * self.deca.config.zsymw

        # else:
        #     uv_texture_gt_patch_ = None
        #     uv_texture_patch_ = None
        #     uv_vis_mask_patch_ = None

        return losses, metrics

    def compute_loss(self, values, training=True) -> (dict, dict):
        losses, metrics = self._compute_loss(values, training=training)

        all_loss = 0.
        losses_key = losses.keys()
        for key in losses_key:
            all_loss = all_loss + losses[key]
        # losses['all_loss'] = all_loss
        losses['loss'] = all_loss

        # add metrics that do not effect the loss function (if any)
        for key in metrics.keys():
            losses['metric_' + key] = metrics[key]
        return losses

    def validation_step(self, batch, batch_idx):
        with torch.no_grad():
            values = self._encode(batch, training=False)
            values = self._decode(values, training=False)
            losses_and_metrics = self.compute_loss(values, training=False)
        self.log_dict(losses_and_metrics, on_step=False, on_epoch=True)
        suffix = str(self.mode.name).lower()
        losses_and_metrics_to_log = {suffix + '_val_' + key: value for key, value in losses_and_metrics.items()}
        self.log_dict(losses_and_metrics_to_log, on_step=False, on_epoch=True)
        return losses_and_metrics


    def test_step(self, batch, batch_idx):
        with torch.no_grad():
            values = self._encode(batch, training=False)
            values = self._decode(values, training=False)
            if 'mask' in batch.keys():
                losses_and_metrics = self.compute_loss(values, training=False)
                suffix = str(self.mode.name).lower()
                losses_and_metrics_to_log = {suffix + '_test_' + key: value for key, value in losses_and_metrics.items()}
                self.log_dict(losses_and_metrics_to_log, on_step=True, on_epoch=False)
            else:
                losses_and_metric = None

        return losses_and_metrics


    def training_step(self, batch, batch_idx): #, debug=True):
        values = self._encode(batch, training=True)
        values = self._decode(values, training=True)
        losses_and_metrics = self.compute_loss(values, training=True)

        uv_detail_normals = None
        if 'uv_detail_normals' in values.keys():
            uv_detail_normals = values['uv_detail_normals']

        # if batch_idx % 200 == 0:
        if self.global_step % 200 == 0:
            self._visualization_checkpoint(values['verts'], values['trans_verts'], values['ops'],
                                           uv_detail_normals, values, batch_idx)
        suffix = str(self.mode.name).lower()
        losses_and_metrics_to_log = {suffix + '_train_' + key: value for key, value in losses_and_metrics.items()}
        self.log_dict(losses_and_metrics_to_log, on_step=False, on_epoch=True)
        return losses_and_metrics

    ### STEP ENDS ARE PROBABLY NOT NECESSARY BUT KEEP AN EYE ON THEM IF MULI-GPU TRAINING DOESN'T WORKs
    # def training_step_end(self, batch_parts):
    #     return self._step_end(batch_parts)
    #
    # def validation_step_end(self, batch_parts):
    #     return self._step_end(batch_parts)
    #
    # def _step_end(self, batch_parts):
    #     # gpu_0_prediction = batch_parts.pred[0]['pred']
    #     # gpu_1_prediction = batch_parts.pred[1]['pred']
    #     N = len(batch_parts)
    #     loss_dict = {}
    #     for key in batch_parts[0]:
    #         for i in range(N):
    #             if key not in loss_dict.keys():
    #                 loss_dict[key] = batch_parts[i]
    #             else:
    #                 loss_dict[key] = batch_parts[i]
    #         loss_dict[key] = loss_dict[key] / N
    #     return loss_dict

    def _visualization_checkpoint(self, verts, trans_verts, ops, uv_detail_normals, additional, batch_idx):
        # visualize
        # if iter % 200 == 1:
        # visind = np.arange(8)  # self.config.batch_size )
        batch_size = verts.shape[0]
        visind = np.arange(batch_size)
        shape_images = self.deca.render.render_shape(verts, trans_verts)
        if uv_detail_normals is not None:
            detail_normal_images = F.grid_sample(uv_detail_normals.detach(), ops['grid'].detach(),
                                                 align_corners=False)
            shape_detail_images = self.deca.render.render_shape(verts, trans_verts,
                                                           detail_normal_images=detail_normal_images)
        else:
            shape_detail_images = None

        visdict = {}
        if 'images' in additional.keys():
            visdict['inputs'] = additional['images'][visind]

        if 'images' in additional.keys() and 'lmk' in additional.keys():
            visdict['landmarks_gt'] = util.tensor_vis_landmarks(additional['images'][visind], additional['lmk'][visind])

        if 'images' in additional.keys() and 'predicted_landmarks' in additional.keys():
            visdict['landmarks_gt'] = util.tensor_vis_landmarks(additional['images'][visind],
                                                                     additional['predicted_landmarks'][visind])
        if 'predicted_images' in additional.keys():
            visdict['predicted_images'] = additional['predicted_images'][visind]

        if 'albedo_images' in additional.keys():
            visdict['albedo_images'] = additional['albedo_images'][visind]

        if 'masks' in additional.keys():
            visdict['mask'] = additional['masks'].repeat(1, 3, 1, 1)[visind]
        if 'albedo' in additional.keys():
            visdict['albedo'] = additional['albedo'][visind]

        if 'predicted_detailed_image' in additional.keys() and additional['predicted_detailed_image'] is not None:
            visdict['detailed_images'] = additional['predicted_detailed_image'][visind]

        if 'shape_detail_images' in additional.keys():
            visdict['shape_detail_images'] = additional['shape_detail_images'][visind]

        if 'uv_detail_normals' in additional.keys():
            visdict['uv_detail_normals'] = additional['uv_detail_normals'][visind] * 0.5 + 0.5

        if 'uv_texture_patch' in additional.keys():
            visdict['uv_texture_patch'] = additional['uv_texture_patch'][visind]

        if 'uv_texture_gt' in additional.keys():
            visdict['uv_texture_gt'] = additional['uv_texture_gt'][visind]

        if 'uv_vis_mask_patch' in additional.keys():
            visdict['uv_vis_mask_patch'] = additional['uv_vis_mask_patch'][visind]

        savepath = '{}/{}/{}_{}.png'.format(self.inout_params.full_run_dir, 'train_images',
                                            self.current_epoch, batch_idx)
        Path(savepath).parent.mkdir(exist_ok=True, parents=True)
        self.deca.visualize(visdict, savepath)

        return visdict

    def _visualization_checkpoint_old(self,  verts, trans_verts, ops, images, lmk, predicted_images,
                                  predicted_landmarks, masks,
                                  albedo, predicted_detailed_image, uv_detail_normals, batch_idx,
                                  uv_texture_patch=None, uv_texture_gt=None, uv_vis_mask_patch=None):
        # visualize
        # if iter % 200 == 1:
        # visind = np.arange(8)  # self.config.batch_size )
        batch_size = verts.shape[0]
        visind = np.arange(batch_size)
        shape_images = self.deca.render.render_shape(verts, trans_verts)
        if uv_detail_normals is not None:
            detail_normal_images = F.grid_sample(uv_detail_normals.detach(), ops['grid'].detach(),
                                                 align_corners=False)
            shape_detail_images = self.deca.render.render_shape(verts, trans_verts,
                                                           detail_normal_images=detail_normal_images)
        else:
            shape_detail_images = None

        visdict = {
            'inputs': images[visind],
            'landmarks_gt': util.tensor_vis_landmarks(images[visind], lmk[visind]),# , isScale=False),
            'landmarks': util.tensor_vis_landmarks(images[visind], predicted_landmarks[visind]),
            'shape': shape_images[visind],
            'predicted_images': predicted_images[visind],
            'albedo_images': ops['albedo_images'][visind],
            'mask': masks.repeat(1, 3, 1, 1)[visind],
            'albedo': albedo[visind],
            # details

        }
        if predicted_detailed_image is not None:
            visdict['detailed_images'] = predicted_detailed_image[visind]
            visdict['shape_detail_images'] = shape_detail_images[visind]
            visdict['detailed_images'] = predicted_detailed_image[visind]
            visdict['uv_detail_normals'] = uv_detail_normals[visind] * 0.5 + 0.5
            visdict['uv_texture_patch'] = uv_texture_patch[visind]
            visdict['uv_texture_gt'] = uv_texture_gt[visind]
            visdict['uv_vis_mask_patch'] = uv_vis_mask_patch[visind]

        savepath = '{}/{}/{}_{}.png'.format(self.inout_params.full_run_dir, 'train_images',
                                            self.current_epoch, batch_idx)
        Path(savepath).parent.mkdir(exist_ok=True, parents=True)
        self.deca.visualize(visdict, savepath)

    # def training_step_end(self, *args, **kwargs):
        # iteration update
        # if iter > all_iter - self.start_iter - 1:
        #     self.start_iter = 0
        #     continue

        # if iter % 500 == 0:
        #     if self.deca.config.multi_gpu:
        #         torch.save(
        #             {
        #                 'E_flame': self.E_flame.module.state_dict(),
        #                 'E_detail': self.E_detail.module.state_dict(),
        #                 'D_detail': self.D_detail.module.state_dict(),
        #                 'opt': self.opt.state_dict(),
        #                 'epoch': epoch,
        #                 'iter': iter,
        #                 'all_iter': all_iter,
        #                 'batch_size': self.config.batch_size
        #             },
        #             os.path.join(self.config.savefolder, 'model' + '.tar')
        #         )
        #     else:
        #         torch.save(
        #             {
        #                 'E_flame': self.deca.E_flame.state_dict(),
        #                 'E_detail': self.deca.E_detail.state_dict(),
        #                 'D_detail': self.deca.D_detail.state_dict(),
        #                 'opt': self.opt.state_dict(),
        #                 'epoch': self.current_epoch,
        #                 # 'iter': iter,
        #                 'all_iter': all_iter,
        #                 'batch_size': self.deca.config.batch_size
        #             },
        #             os.path.join(self.deca.config.savefolder, 'model' + '.tar')
        #         )

    # def on_save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
    #     pass
    #     checkpoint['epoch'] = self.current_epoch
    #     checkpoint['iter'] = -1 # to be deprecated
    #     checkpoint['all_iter'] = -1 # to be deprecated
    #     checkpoint['batch_size'] = self.deca.config.batch_size_train
    #

    def configure_optimizers(self):
        # optimizer = torch.optim.Adam(self.parameters(), lr=1e-3)
        trainable_params = []
        if self.mode == DecaMode.COARSE:
            trainable_params += list(self.deca.E_flame.parameters())
        elif self.mode == DecaMode.DETAIL:
            trainable_params += list(self.deca.E_detail.parameters())
            trainable_params += list(self.deca.D_detail.parameters())
        else:
            raise ValueError(f"Invalid deca mode: {self.mode}")

        if self.learning_params.optimizer == 'Adam':
            self.deca.opt = torch.optim.Adam(
                trainable_params,
                lr=self.learning_params.learning_rate,
                amsgrad=False)

        elif self.learning_params.optimizer == 'SGD':
            self.deca.opt = torch.optim.SGD(
                trainable_params,
                lr=self.learning_params.learning_rate)
        return self.deca.opt




class DECA(torch.nn.Module):
    def __init__(self, config):
        super().__init__()
        self._reconfigure(config)
        self._reinitialize()

    def _reconfigure(self, config):
        self.config = config
        self.n_param = config.n_shape + config.n_tex + config.n_exp + config.n_pose + config.n_cam + config.n_light
        self.n_detail = config.n_detail
        self.n_cond = 3 + config.n_exp

    def _reinitialize(self):
        self._create_model()
        self._setup_renderer()

        self.perceptual_loss = lossfunc.IDMRFLoss()
        self.id_loss = lossfunc.VGGFace2Loss(self.config.pretrained_vgg_face_path)
        self.face_attr_mask = util.load_local_mask(image_size=self.config.uv_size, mode='bbx')

    def _setup_renderer(self):
        self.render = SRenderY(self.config.image_size, obj_filename=self.config.topology_path,
                               uv_size=self.config.uv_size)  # .to(self.device)
        # face mask for rendering details
        mask = imread(self.config.face_mask_path).astype(np.float32) / 255.;
        mask = torch.from_numpy(mask[:, :, 0])[None, None, :, :].contiguous()
        self.uv_face_mask = F.interpolate(mask, [self.config.uv_size, self.config.uv_size])
        mask = imread(self.config.face_eye_mask_path).astype(np.float32) / 255.;
        mask = torch.from_numpy(mask[:, :, 0])[None, None, :, :].contiguous()
        self.uv_face_eye_mask = F.interpolate(mask, [self.config.uv_size, self.config.uv_size])
        ## displacement correct
        if os.path.isfile(self.config.fixed_displacement_path):
            fixed_dis = np.load(self.config.fixed_displacement_path)
            self.fixed_uv_dis = torch.tensor(fixed_dis).float()
        else:
            self.fixed_uv_dis = torch.zeros([512, 512]).float()

    def _create_model(self):
        # coarse shape
        self.E_flame = ResnetEncoder(outsize=self.n_param)
        self.flame = FLAME(self.config)
        self.flametex = FLAMETex(self.config)
        # detail modeling
        self.E_detail = ResnetEncoder(outsize=self.n_detail)
        self.D_detail = Generator(latent_dim=self.n_detail + self.n_cond, out_channels=1, out_scale=0.01,
                                  sample_mode='bilinear')

        if self.config.resume_training:
            model_path = self.config.pretrained_modelpath
            print('trained model found. load {}'.format(model_path))
            checkpoint = torch.load(model_path)
            # model
            util.copy_state_dict(self.E_flame.state_dict(), checkpoint['E_flame'])
            # util.copy_state_dict(self.opt.state_dict(), checkpoint['opt']) # deprecate
            # detail model
            if 'E_detail' in checkpoint.keys():
                util.copy_state_dict(self.E_detail.state_dict(), checkpoint['E_detail'])
                util.copy_state_dict(self.D_detail.state_dict(), checkpoint['D_detail'])
            # training state
            self.start_epoch = 0  # checkpoint['epoch']
            self.start_iter = 0  # checkpoint['iter']
        else:
            print('Start training from scratch')
            self.start_epoch = 0
            self.start_iter = 0

    def decompose_code(self, code):
        '''
        config.n_shape + config.n_tex + config.n_exp + config.n_pose + config.n_cam + config.n_light
        '''
        code_list = []
        num_list = [self.config.n_shape, self.config.n_tex, self.config.n_exp, self.config.n_pose, self.config.n_cam,
                    self.config.n_light]
        start = 0
        for i in range(len(num_list)):
            code_list.append(code[:, start:start + num_list[i]])
            start = start + num_list[i]
        # shapecode, texcode, expcode, posecode, cam, lightcode = code_list
        code_list[-1] = code_list[-1].reshape(code.shape[0], 9, 3)
        return code_list

    def displacement2normal(self, uv_z, coarse_verts, coarse_normals):
        batch_size = uv_z.shape[0]
        uv_coarse_vertices = self.render.world2uv(coarse_verts).detach()
        uv_coarse_normals = self.render.world2uv(coarse_normals).detach()

        uv_z = uv_z * self.uv_face_eye_mask
        uv_detail_vertices = uv_coarse_vertices + uv_z * uv_coarse_normals + self.fixed_uv_dis[None, None, :,
                                                                             :] * uv_coarse_normals.detach()
        dense_vertices = uv_detail_vertices.permute(0, 2, 3, 1).reshape([batch_size, -1, 3])
        uv_detail_normals = util.vertex_normals(dense_vertices, self.render.dense_faces.expand(batch_size, -1, -1))
        uv_detail_normals = uv_detail_normals.reshape(
            [batch_size, uv_coarse_vertices.shape[2], uv_coarse_vertices.shape[3], 3]).permute(0, 3, 1, 2)
        # uv_detail_normals = uv_detail_normals*self.uv_face_eye_mask + uv_coarse_normals*(1-self.uv_face_eye_mask)
        # uv_detail_normals = util.gaussian_blur(uv_detail_normals)
        return uv_detail_normals, uv_coarse_vertices

    def visualize(self, visdict, savepath):
        grids = {}
        for key in visdict:
            # print(key)
            if visdict[key] is None:
                continue
            grids[key] = torchvision.utils.make_grid(
                F.interpolate(visdict[key], [self.config.image_size, self.config.image_size])).detach().cpu()
        grid = torch.cat(list(grids.values()), 1)
        grid_image = (grid.numpy().transpose(1, 2, 0).copy() * 255)[:, :, [2, 1, 0]]
        grid_image = np.minimum(np.maximum(grid_image, 0), 255).astype(np.uint8)
        cv2.imwrite(savepath, grid_image)

    # deprecate
    def test(self, n_person=None, testpath=None, scale=None, iscrop=None, return_params=False, vispath=None,
             kptfolder=None):
        if self.config.test_data == 'vox1':
            testdata = VoxelDataset(K=self.config.K, image_size=self.config.image_size,
                                    scale=[self.config.scale_min, self.config.scale_max], isEval=True)
        elif self.config.test_data == 'testdata':
            if testpath is None:
                testpath = self.config.testpath
            if scale is None:
                scale = (self.config.scale_min + self.config.scale_max) / 2.
            if iscrop is None:
                iscrop = self.config.iscrop
            if kptfolder is None:
                testdata = TestData(testpath, iscrop=iscrop, crop_size=224, scale=scale)
            else:
                testdata = EvalData(testpath, kptfolder, iscrop=iscrop, crop_size=224, scale=scale)

        else:
            print('please check test data')
            exit()

        ## train model
        self.E_flame.eval()  # self.M.train(); self.G.train()
        self.E_detail.eval()
        self.D_detail.eval()

        if n_person is None or n_person > len(testdata):
            n_person = len(testdata)

        for i in range(n_person):
            images = testdata[i]['image']  # .to(self.device)[None,...]
            images = images.view(-1, images.shape[-3], images.shape[-2], images.shape[-1])
            batch_size = images.shape[0]

            # -- encoder
            with torch.no_grad():
                parameters = self.E_flame(images)
                detailcode = self.E_detail(images)

            code_list = self.decompose_code(parameters)
            shapecode, texcode, expcode, posecode, cam, lightcode = code_list

            # -- decoder
            # FLAME
            verts, landmarks2d, landmarks3d = self.flame(shape_params=shapecode, expression_params=expcode,
                                                         pose_params=posecode)
            predicted_landmarks = util.batch_orth_proj(landmarks2d, cam)[:, :, :2];
            predicted_landmarks[:, :, 1:] = - predicted_landmarks[:, :, 1:]
            trans_verts = util.batch_orth_proj(verts, cam);
            trans_verts[:, :, 1:] = -trans_verts[:, :, 1:]
            albedo = self.flametex(texcode)

            # Detail
            uv_z = self.D_detail(torch.cat([posecode[:, 3:], expcode, detailcode], dim=1))

            # ------ rendering
            # import ipdb; ipdb.set_trace()
            ops = self.render(verts, trans_verts, albedo, lightcode)
            # mask
            mask_face_eye = F.grid_sample(self.uv_face_eye_mask.expand(batch_size, -1, -1, -1), ops['grid'].detach(),
                                          align_corners=False)
            # images
            predicted_images = ops['images'] * mask_face_eye * ops['alpha_images']

            # predicted_images_G = self.render(verts.detach(), trans_verts.detach(), albedo_G, lightcode.detach())['images']
            visind = np.arange(self.config.K)
            shape_images = self.render.render_shape(verts, trans_verts)

            # render detail
            uv_detail_normals, uv_coarse_vertices = self.displacement2normal(uv_z, verts, ops['normals'])
            detail_normal_images = F.grid_sample(uv_detail_normals, ops['grid'], align_corners=False)
            shape_detail_images = self.render.render_shape(verts, trans_verts,
                                                           detail_normal_images=detail_normal_images)
            uv_shading = self.render.add_SHlight(uv_detail_normals, lightcode)
            uv_texture = albedo * uv_shading
            predicted_detailed_image = F.grid_sample(uv_texture, ops['grid'], align_corners=False)

            visdict = {
                'inputs': images[visind],
                'landmarks': util.tensor_vis_landmarks(images[visind], predicted_landmarks[visind]),
                'shape': shape_images[visind],
                'detail_shape': shape_detail_images[visind],
                'predicted_images': predicted_images[visind],
                'detail_images': predicted_detailed_image[visind],
                # 'albedo_images': ops['albedo_images'][visind],
                'albedo': albedo[visind],
            }
            if vispath is None:
                vispath_curr = '{}/{}/{}.jpg'.format(self.config.savefolder, self.config.dataname, i)
            self.visualize(visdict, vispath_curr)
            print('{}/{}: '.format(i, n_person), vispath_curr)
            if return_params:
                param_dict = {
                    'shapecode': shapecode,
                    'expcode': expcode,
                    'posecode': posecode,
                    'cam': cam,
                    'texcode': texcode,
                    'lightcode': lightcode,
                    'detailcode': detailcode
                }
                return param_dict