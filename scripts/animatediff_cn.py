from pathlib import Path
from types import MethodType
from typing import Optional

import os
import shutil
import cv2
import numpy as np
import torch
from tqdm import tqdm
from PIL import Image, ImageFilter, ImageOps
from modules import processing, shared, masking, images, devices
from modules.paths import data_path
from modules.processing import (StableDiffusionProcessing,
                                StableDiffusionProcessingImg2Img,
                                StableDiffusionProcessingTxt2Img)

from scripts.animatediff_logger import logger_animatediff as logger
from scripts.animatediff_ui import AnimateDiffProcess
from scripts.animatediff_prompt import AnimateDiffPromptSchedule
from scripts.animatediff_infotext import update_infotext
from scripts.animatediff_i2ibatch import animatediff_i2ibatch


class AnimateDiffControl:
    original_processing_process_images_hijack = None
    original_controlnet_main_entry = None
    original_postprocess_batch = None

    def __init__(self, p: StableDiffusionProcessing, prompt_scheduler: AnimateDiffPromptSchedule):
        try:
            from scripts.external_code import find_cn_script
            self.cn_script = find_cn_script(p.scripts)
        except:
            self.cn_script = None
        self.prompt_scheduler = prompt_scheduler


    def hack_batchhijack(self, params: AnimateDiffProcess):
        cn_script = self.cn_script
        prompt_scheduler = self.prompt_scheduler

        def get_input_frames():
            if params.video_source is not None and params.video_source != '':
                cap = cv2.VideoCapture(params.video_source)
                frame_count = 0
                tmp_frame_dir = Path(f'{data_path}/tmp/animatediff-frames/')
                tmp_frame_dir.mkdir(parents=True, exist_ok=True)
                while cap.isOpened():
                    ret, frame = cap.read()
                    if not ret:
                        break
                    cv2.imwrite(f"{tmp_frame_dir}/{frame_count}.png", frame)
                    frame_count += 1
                cap.release()
                return str(tmp_frame_dir)
            elif params.video_path is not None and params.video_path != '':
                return params.video_path
            return ''

        from scripts.batch_hijack import BatchHijack, instance
        def hacked_processing_process_images_hijack(self, p: StableDiffusionProcessing, *args, **kwargs):
            from scripts import external_code
            from scripts.batch_hijack import InputMode

            units = external_code.get_all_units_in_processing(p)
            units = [unit for unit in units if getattr(unit, 'enabled', False)]

            if len(units) > 0:
                global_input_frames = get_input_frames()
                for idx, unit in enumerate(units):
                    # i2i-batch mode
                    if getattr(p, '_animatediff_i2i_batch', None) and not unit.image:
                        unit.input_mode = InputMode.BATCH
                    # if no input given for this unit, use global input
                    if getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH:
                        if not unit.batch_images:
                            assert global_input_frames, 'No input images found for ControlNet module'
                            unit.batch_images = global_input_frames
                    elif not unit.image:
                        try:
                            cn_script.choose_input_image(p, unit, idx)
                        except:
                            assert global_input_frames != '', 'No input images found for ControlNet module'
                            unit.batch_images = global_input_frames
                            unit.input_mode = InputMode.BATCH

                    if getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH:
                        if 'inpaint' in unit.module:
                            images = shared.listfiles(f'{unit.batch_images}/image')
                            masks = shared.listfiles(f'{unit.batch_images}/mask')
                            assert len(images) == len(masks), 'Inpainting image mask count mismatch'
                            unit.batch_images = [{'image': images[i], 'mask': masks[i]} for i in range(len(images))]
                        else:
                            unit.batch_images = shared.listfiles(unit.batch_images)

                unit_batch_list = [len(unit.batch_images) for unit in units
                                   if getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH]
                if getattr(p, '_animatediff_i2i_batch', None):
                    unit_batch_list.append(len(p.init_images))

                if len(unit_batch_list) > 0:
                    video_length = min(unit_batch_list)
                    # ensure that params.video_length <= video_length and params.batch_size <= video_length
                    if params.video_length > video_length:
                        params.video_length = video_length
                    if params.batch_size > video_length:
                        params.batch_size = video_length
                    if params.video_default:
                        params.video_length = video_length
                        p.batch_size = video_length
                    for unit in units:
                        if getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH:
                            unit.batch_images = unit.batch_images[:params.video_length]

            animatediff_i2ibatch.cap_init_image(p, params)
            prompt_scheduler.parse_prompt(p, params)
            update_infotext(p, params)
            return getattr(processing, '__controlnet_original_process_images_inner')(p, *args, **kwargs)
        
        if AnimateDiffControl.original_processing_process_images_hijack is not None:
            logger.info('BatchHijack already hacked.')
            return

        AnimateDiffControl.original_processing_process_images_hijack = BatchHijack.processing_process_images_hijack
        BatchHijack.processing_process_images_hijack = hacked_processing_process_images_hijack
        processing.process_images_inner = instance.processing_process_images_hijack


    def restore_batchhijack(self):
        if AnimateDiffControl.original_processing_process_images_hijack is not None:
            from scripts.batch_hijack import BatchHijack, instance
            BatchHijack.processing_process_images_hijack = AnimateDiffControl.original_processing_process_images_hijack
            AnimateDiffControl.original_processing_process_images_hijack = None
            processing.process_images_inner = instance.processing_process_images_hijack


    def hack_cn(self):
        cn_script = self.cn_script


        def hacked_main_entry(self, p: StableDiffusionProcessing, batch_option_uint_separate="All ControlNet units for all images in a batch", batch_option_style_align=False):
            from scripts import external_code, global_state, hook
            from scripts.controlnet_lora import bind_control_lora
            from scripts.adapter import Adapter, Adapter_light, StyleAdapter
            from scripts.batch_hijack import InputMode
            from scripts.controlnet_lllite import PlugableControlLLLite, clear_all_lllite
            from scripts.controlmodel_ipadapter import (PlugableIPAdapter,
                                                        clear_all_ip_adapter)
            from scripts.hook import ControlParams, UnetHook
            from scripts.enums import ControlModelType
            from scripts.logging import logger
            from scripts.processor import model_free_preprocessors

            # TODO: i2i-batch mode, what should I change?
            def image_has_mask(input_image: np.ndarray) -> bool:
                return (
                    input_image.ndim == 3 and 
                    input_image.shape[2] == 4 and 
                    np.max(input_image[:, :, 3]) > 127
                )


            def prepare_mask(
                mask: Image.Image, p: processing.StableDiffusionProcessing
            ) -> Image.Image:
                mask = mask.convert("L")
                if getattr(p, "inpainting_mask_invert", False):
                    mask = ImageOps.invert(mask)
                
                if hasattr(p, 'mask_blur_x'):
                    if getattr(p, "mask_blur_x", 0) > 0:
                        np_mask = np.array(mask)
                        kernel_size = 2 * int(2.5 * p.mask_blur_x + 0.5) + 1
                        np_mask = cv2.GaussianBlur(np_mask, (kernel_size, 1), p.mask_blur_x)
                        mask = Image.fromarray(np_mask)
                    if getattr(p, "mask_blur_y", 0) > 0:
                        np_mask = np.array(mask)
                        kernel_size = 2 * int(2.5 * p.mask_blur_y + 0.5) + 1
                        np_mask = cv2.GaussianBlur(np_mask, (1, kernel_size), p.mask_blur_y)
                        mask = Image.fromarray(np_mask)
                else:
                    if getattr(p, "mask_blur", 0) > 0:
                        mask = mask.filter(ImageFilter.GaussianBlur(p.mask_blur))
                
                return mask


            def set_numpy_seed(p: processing.StableDiffusionProcessing) -> Optional[int]:
                try:
                    tmp_seed = int(p.all_seeds[0] if p.seed == -1 else max(int(p.seed), 0))
                    tmp_subseed = int(p.all_seeds[0] if p.subseed == -1 else max(int(p.subseed), 0))
                    seed = (tmp_seed + tmp_subseed) & 0xFFFFFFFF
                    np.random.seed(seed)
                    return seed
                except Exception as e:
                    logger.warning(e)
                    logger.warning('Warning: Failed to use consistent random seed.')
                    return None

            sd_ldm = p.sd_model
            unet = sd_ldm.model.diffusion_model
            self.noise_modifier = None

            setattr(p, 'controlnet_control_loras', [])

            if self.latest_network is not None:
                # always restore (~0.05s)
                self.latest_network.restore()

            # always clear (~0.05s)
            clear_all_lllite()
            clear_all_ip_adapter()

            self.enabled_units = cn_script.get_enabled_units(p)

            if len(self.enabled_units) == 0:
                self.latest_network = None
                return

            detected_maps = []
            forward_params = []
            post_processors = []

            # cache stuff
            if self.latest_model_hash != p.sd_model.sd_model_hash:
                cn_script.clear_control_model_cache()

            for idx, unit in enumerate(self.enabled_units):
                unit.module = global_state.get_module_basename(unit.module)

            # unload unused preproc
            module_list = [unit.module for unit in self.enabled_units]
            for key in self.unloadable:
                if key not in module_list:
                    self.unloadable.get(key, lambda:None)()

            self.latest_model_hash = p.sd_model.sd_model_hash
            for idx, unit in enumerate(self.enabled_units):
                cn_script.bound_check_params(unit)
                cn_script.check_sd_version_compatible(unit)

                resize_mode = external_code.resize_mode_from_value(unit.resize_mode)
                control_mode = external_code.control_mode_from_value(unit.control_mode)

                if unit.module in model_free_preprocessors:
                    model_net = None
                    if 'reference' in unit.module:
                        control_model_type = ControlModelType.AttentionInjection
                    elif 'revision' in unit.module:
                        control_model_type = ControlModelType.ReVision
                    else:
                        raise Exception("Unable to determine control_model_type.")
                else:
                    model_net, control_model_type = cn_script.load_control_model(p, unet, unit.model)
                    model_net.reset()
                    if model_net is not None and getattr(devices, "fp8", False) and not isinstance(model_net, PlugableIPAdapter):
                        for _module in model_net.modules():
                            if isinstance(_module, (torch.nn.Conv2d, torch.nn.Linear)):
                                _module.to(torch.float8_e4m3fn)

                    if getattr(model_net, 'is_control_lora', False):
                        control_lora = model_net.control_model
                        bind_control_lora(unet, control_lora)
                        p.controlnet_control_loras.append(control_lora)
                        
                    if control_model_type == ControlModelType.ControlLoRA:
                        control_lora = model_net.control_model
                        bind_control_lora(unet, control_lora)
                        p.controlnet_control_loras.append(control_lora)
                        # Change control_model_type to ControlNet as all processes
                        # in hook.py still want the ControlNetLoRA to be treated
                        # the same way as ControlNet.
                        control_model_type = ControlModelType.ControlNet
                
                h, w, hr_y, hr_x = cn_script.get_target_dimensions(p)

                if getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH:
                    input_images = []
                    for img in unit.batch_images:
                        unit.image = img
                        input_image, resize_mode = cn_script.choose_input_image(p, unit, idx)
                        input_images.append(input_image)
                else:
                    input_image, resize_mode = cn_script.choose_input_image(p, unit, idx)
                    input_images = [input_image]

                for idx, input_image in enumerate(input_images):
                    input_image = cn_script.try_crop_image_with_a1111_mask(p, unit, input_image, resize_mode)
                    input_image = np.ascontiguousarray(input_image.copy()).copy() # safe numpy
                    if unit.module == 'inpaint_only+lama' and resize_mode == external_code.ResizeMode.OUTER_FIT:
                        # inpaint_only+lama is special and required outpaint fix
                        _, input_image = cn_script.detectmap_proc(input_image, unit.module, resize_mode, hr_y, hr_x)
                    if unit.pixel_perfect:
                        unit.processor_res = external_code.pixel_perfect_resolution(
                            input_image,
                            target_H=h,
                            target_W=w,
                            resize_mode=resize_mode,
                        )
                    input_images[idx] = input_image

                if 'inpaint_only' == unit.module and issubclass(type(p), StableDiffusionProcessingImg2Img) and p.image_mask is not None:
                    logger.warning('A1111 inpaint and ControlNet inpaint duplicated. ControlNet support enabled.')
                    unit.module = 'inpaint'

                logger.info(f"Loading preprocessor: {unit.module}")
                preprocessor = self.preprocessor[unit.module]

                high_res_fix = isinstance(p, StableDiffusionProcessingTxt2Img) and getattr(p, 'enable_hr', False)


                if unit.module == 'inpaint_only+lama' and resize_mode == external_code.ResizeMode.OUTER_FIT:
                    # inpaint_only+lama is special and required outpaint fix
                    for idx, input_image in enumerate(input_images):
                        _, input_image = cn_script.detectmap_proc(input_image, unit.module, resize_mode, hr_y, hr_x)
                        input_images[idx] = input_image

                control_model_type = ControlModelType.ControlNet
                global_average_pooling = False

                if 'reference' in unit.module:
                    control_model_type = ControlModelType.AttentionInjection
                elif 'revision' in unit.module:
                    control_model_type = ControlModelType.ReVision
                elif hasattr(model_net, 'control_model') and (isinstance(model_net.control_model, Adapter) or isinstance(model_net.control_model, Adapter_light)):
                    control_model_type = ControlModelType.T2I_Adapter
                elif hasattr(model_net, 'control_model') and isinstance(model_net.control_model, StyleAdapter):
                    control_model_type = ControlModelType.T2I_StyleAdapter
                elif isinstance(model_net, PlugableIPAdapter):
                    control_model_type = ControlModelType.IPAdapter
                elif isinstance(model_net, PlugableControlLLLite):
                    control_model_type = ControlModelType.Controlllite

                if control_model_type is ControlModelType.ControlNet:
                    global_average_pooling = model_net.control_model.global_average_pooling

                preprocessor_resolution = unit.processor_res
                if unit.pixel_perfect:
                    preprocessor_resolution = external_code.pixel_perfect_resolution(
                        input_images[0],
                        target_H=h,
                        target_W=w,
                        resize_mode=resize_mode
                    )

                logger.info(f'preprocessor resolution = {preprocessor_resolution}')
                # Preprocessor result may depend on numpy random operations, use the
                # random seed in `StableDiffusionProcessing` to make the 
                # preprocessor result reproducable.
                # Currently following preprocessors use numpy random:
                # - shuffle
                seed = set_numpy_seed(p)
                logger.debug(f"Use numpy seed {seed}.")

                controls = []
                hr_controls = []
                controls_ipadapter = {'hidden_states': [], 'image_embeds': []}
                hr_controls_ipadapter = {'hidden_states': [], 'image_embeds': []}
                for idx, input_image in tqdm(enumerate(input_images), total=len(input_images)):
                    detected_map, is_image = preprocessor(
                        input_image, 
                        res=preprocessor_resolution, 
                        thr_a=unit.threshold_a,
                        thr_b=unit.threshold_b,
                    )

                    if high_res_fix:
                        if is_image:
                            hr_control, hr_detected_map = cn_script.detectmap_proc(detected_map, unit.module, resize_mode, hr_y, hr_x)
                            detected_maps.append((hr_detected_map, unit.module))
                        else:
                            hr_control = detected_map
                    else:
                        hr_control = None

                    if is_image:
                        control, detected_map = cn_script.detectmap_proc(detected_map, unit.module, resize_mode, h, w)
                        detected_maps.append((detected_map, unit.module))
                    else:
                        control = detected_map
                        detected_maps.append((input_image, unit.module))

                    if control_model_type == ControlModelType.T2I_StyleAdapter:
                        control = control['last_hidden_state']

                    if control_model_type == ControlModelType.ReVision:
                        control = control['image_embeds']

                    if control_model_type == ControlModelType.IPAdapter:
                        if model_net.is_plus:
                            controls_ipadapter['hidden_states'].append(control['hidden_states'][-2].cpu())
                        elif unit.module == 'ip-adapter_face_id':
                            controls.append(control[0])
                        else:
                            controls_ipadapter['image_embeds'].append(control['image_embeds'].cpu())
                        if hr_control is not None:
                            if model_net.is_plus:
                                hr_controls_ipadapter['hidden_states'].append(hr_control['hidden_states'][-2].cpu())
                            elif unit.module == 'ip-adapter_face_id':
                                hr_controls.append(control[0])
                            else:
                                hr_controls_ipadapter['image_embeds'].append(hr_control['image_embeds'].cpu())
                        else:
                            hr_controls_ipadapter = None
                            hr_controls = None
                    else:
                        controls.append(control.cpu())
                        if hr_control is not None:
                            hr_controls.append(hr_control.cpu())
                        else:
                            hr_controls = None
                
                if control_model_type == ControlModelType.IPAdapter and unit.module != 'ip-adapter_face_id':
                    ipadapter_key = 'hidden_states' if model_net.is_plus else 'image_embeds'
                    controls = {ipadapter_key: torch.cat(controls_ipadapter[ipadapter_key], dim=0)}
                    if controls[ipadapter_key].shape[0] > 1:
                        controls[ipadapter_key] = torch.cat([controls[ipadapter_key], controls[ipadapter_key]], dim=0)
                    if model_net.is_plus:
                        controls[ipadapter_key] = [controls[ipadapter_key], None]
                    if hr_controls_ipadapter is not None:
                        hr_controls = {ipadapter_key: torch.cat(hr_controls_ipadapter[ipadapter_key], dim=0)}
                        if hr_controls[ipadapter_key].shape[0] > 1:
                            hr_controls[ipadapter_key] = torch.cat([hr_controls[ipadapter_key], hr_controls[ipadapter_key]], dim=0)
                        if model_net.is_plus:
                            hr_controls[ipadapter_key] = [hr_controls[ipadapter_key], None]
                else:
                    controls = torch.cat(controls, dim=0)
                    if controls.shape[0] > 1:
                        controls = torch.cat([controls, controls], dim=0)
                    if hr_controls is not None:
                        hr_controls = torch.cat(hr_controls, dim=0)
                        if hr_controls.shape[0] > 1:
                            hr_controls = torch.cat([hr_controls, hr_controls], dim=0)
                if unit.module == 'ip-adapter_face_id':
                    controls = [controls]
                    if hr_controls is not None:
                        hr_controls = [hr_controls]

                preprocessor_dict = dict(
                    name=unit.module,
                    preprocessor_resolution=preprocessor_resolution,
                    threshold_a=unit.threshold_a,
                    threshold_b=unit.threshold_b
                )

                forward_param = ControlParams(
                    control_model=model_net,
                    preprocessor=preprocessor_dict,
                    hint_cond=controls,
                    weight=unit.weight,
                    guidance_stopped=False,
                    start_guidance_percent=unit.guidance_start,
                    stop_guidance_percent=unit.guidance_end,
                    advanced_weighting=None,
                    control_model_type=control_model_type,
                    global_average_pooling=global_average_pooling,
                    hr_hint_cond=hr_controls,
                    soft_injection=control_mode != external_code.ControlMode.BALANCED,
                    cfg_injection=control_mode == external_code.ControlMode.CONTROL,
                )
                forward_params.append(forward_param)

                unit_is_batch = getattr(unit, 'input_mode', InputMode.SIMPLE) == InputMode.BATCH
                if 'inpaint_only' in unit.module:
                    final_inpaint_raws = []
                    final_inpaint_masks = []
                    for i in range(len(controls)):
                        final_inpaint_feed = hr_controls[i] if hr_controls is not None else controls[i]
                        final_inpaint_feed = final_inpaint_feed.detach().cpu().numpy()
                        final_inpaint_feed = np.ascontiguousarray(final_inpaint_feed).copy()
                        final_inpaint_mask = final_inpaint_feed[0, 3, :, :].astype(np.float32)
                        final_inpaint_raw = final_inpaint_feed[0, :3].astype(np.float32)
                        sigma = shared.opts.data.get("control_net_inpaint_blur_sigma", 7)
                        final_inpaint_mask = cv2.dilate(final_inpaint_mask, np.ones((sigma, sigma), dtype=np.uint8))
                        final_inpaint_mask = cv2.blur(final_inpaint_mask, (sigma, sigma))[None]
                        _, Hmask, Wmask = final_inpaint_mask.shape
                        final_inpaint_raw = torch.from_numpy(np.ascontiguousarray(final_inpaint_raw).copy())
                        final_inpaint_mask = torch.from_numpy(np.ascontiguousarray(final_inpaint_mask).copy())
                        final_inpaint_raws.append(final_inpaint_raw)
                        final_inpaint_masks.append(final_inpaint_mask)

                    def inpaint_only_post_processing(x, i):
                        _, H, W = x.shape
                        if Hmask != H or Wmask != W:
                            logger.error('Error: ControlNet find post-processing resolution mismatch. This could be related to other extensions hacked processing.')
                            return x
                        idx = i if unit_is_batch else 0
                        r = final_inpaint_raw[idx].to(x.dtype).to(x.device)
                        m = final_inpaint_mask[idx].to(x.dtype).to(x.device)
                        y = m * x.clip(0, 1) + (1 - m) * r
                        y = y.clip(0, 1)
                        return y

                    post_processors.append(inpaint_only_post_processing)

                if 'recolor' in unit.module:
                    final_feeds = []
                    for i in range(len(controls)):
                        final_feed = hr_control if hr_control is not None else control
                        final_feed = final_feed.detach().cpu().numpy()
                        final_feed = np.ascontiguousarray(final_feed).copy()
                        final_feed = final_feed[0, 0, :, :].astype(np.float32)
                        final_feed = (final_feed * 255).clip(0, 255).astype(np.uint8)
                        Hfeed, Wfeed = final_feed.shape
                        final_feeds.append(final_feed)

                    if 'luminance' in unit.module:

                        def recolor_luminance_post_processing(x, i):
                            C, H, W = x.shape
                            if Hfeed != H or Wfeed != W or C != 3:
                                logger.error('Error: ControlNet find post-processing resolution mismatch. This could be related to other extensions hacked processing.')
                                return x
                            h = x.detach().cpu().numpy().transpose((1, 2, 0))
                            h = (h * 255).clip(0, 255).astype(np.uint8)
                            h = cv2.cvtColor(h, cv2.COLOR_RGB2LAB)
                            h[:, :, 0] = final_feed[i if unit_is_batch else 0]
                            h = cv2.cvtColor(h, cv2.COLOR_LAB2RGB)
                            h = (h.astype(np.float32) / 255.0).transpose((2, 0, 1))
                            y = torch.from_numpy(h).clip(0, 1).to(x)
                            return y

                        post_processors.append(recolor_luminance_post_processing)

                    if 'intensity' in unit.module:

                        def recolor_intensity_post_processing(x, i):
                            C, H, W = x.shape
                            if Hfeed != H or Wfeed != W or C != 3:
                                logger.error('Error: ControlNet find post-processing resolution mismatch. This could be related to other extensions hacked processing.')
                                return x
                            h = x.detach().cpu().numpy().transpose((1, 2, 0))
                            h = (h * 255).clip(0, 255).astype(np.uint8)
                            h = cv2.cvtColor(h, cv2.COLOR_RGB2HSV)
                            h[:, :, 2] = final_feed[i if unit_is_batch else 0]
                            h = cv2.cvtColor(h, cv2.COLOR_HSV2RGB)
                            h = (h.astype(np.float32) / 255.0).transpose((2, 0, 1))
                            y = torch.from_numpy(h).clip(0, 1).to(x)
                            return y

                        post_processors.append(recolor_intensity_post_processing)

                if '+lama' in unit.module:
                    forward_param.used_hint_cond_latent = hook.UnetHook.call_vae_using_process(p, control)
                    self.noise_modifier = forward_param.used_hint_cond_latent

                del model_net

            is_low_vram = any(unit.low_vram for unit in self.enabled_units)

            self.latest_network = UnetHook(lowvram=is_low_vram)
            self.latest_network.hook(model=unet, sd_ldm=sd_ldm, control_params=forward_params, process=p)

            for param in forward_params:
                if param.control_model_type == ControlModelType.IPAdapter:
                    param.control_model.hook(
                        model=unet,
                        clip_vision_output=param.hint_cond,
                        weight=param.weight,
                        dtype=torch.float32,
                        start=param.start_guidance_percent,
                        end=param.stop_guidance_percent
                    ) 
                if param.control_model_type == ControlModelType.Controlllite:
                    param.control_model.hook(
                        model=unet,
                        cond=param.hint_cond,
                        weight=param.weight,
                        start=param.start_guidance_percent,
                        end=param.stop_guidance_percent
                    )

            self.detected_map = detected_maps
            self.post_processors = post_processors

            if os.path.exists(f'{data_path}/tmp/animatediff-frames/'):
                shutil.rmtree(f'{data_path}/tmp/animatediff-frames/')

        def hacked_postprocess_batch(self, p, *args, **kwargs):
            images = kwargs.get('images', [])
            for post_processor in self.post_processors:
                for i in range(len(images)):
                    images[i] = post_processor(images[i], i)
            return

        if AnimateDiffControl.original_controlnet_main_entry is not None:
            logger.info('ControlNet Main Entry already hacked.')
            return

        AnimateDiffControl.original_controlnet_main_entry = self.cn_script.controlnet_main_entry
        AnimateDiffControl.original_postprocess_batch = self.cn_script.postprocess_batch
        self.cn_script.controlnet_main_entry = MethodType(hacked_main_entry, self.cn_script)
        self.cn_script.postprocess_batch = MethodType(hacked_postprocess_batch, self.cn_script)


    def restore_cn(self):
        if AnimateDiffControl.original_controlnet_main_entry is not None:
            self.cn_script.controlnet_main_entry = AnimateDiffControl.original_controlnet_main_entry
            AnimateDiffControl.original_controlnet_main_entry = None
        if AnimateDiffControl.original_postprocess_batch is not None:
            self.cn_script.postprocess_batch = AnimateDiffControl.original_postprocess_batch
            AnimateDiffControl.original_postprocess_batch = None


    def hack(self, params: AnimateDiffProcess):
        if self.cn_script is not None:
            logger.info(f"Hacking ControlNet.")
            self.hack_batchhijack(params)
            self.hack_cn()


    def restore(self):
        if self.cn_script is not None:
            logger.info(f"Restoring ControlNet.")
            self.restore_batchhijack()
            self.restore_cn()
