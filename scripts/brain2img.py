# command: python ./StableDiffusionFork/scripts/brain2img.py --n_samples 1 --ckpt /home/jacob/projects/DeepLearningFinalProject/StableDiffusionFork/checkpoints/512-base-ema.ckpt --config ./StableDiffusionFork/configs/stable-diffusion/v2-inference.yaml
import argparse, os
import cv2
import torch
import numpy as np
from omegaconf import OmegaConf
from PIL import Image
from tqdm import tqdm, trange
from itertools import islice
from einops import rearrange
from torchvision.utils import make_grid
from pytorch_lightning import seed_everything
from torch import autocast
from contextlib import nullcontext
import fsspec
from ldm.util import instantiate_from_config
from ldm.models.diffusion.ddim import DDIMSampler
from ldm.models.diffusion.plms import PLMSSampler
from ldm.models.diffusion.dpm_solver import DPMSolverSampler

from training.networks import BrainScanEmbedder
from training.configs.configs import Snapshot, ModelConfig
import json

torch.set_grad_enabled(False)


def chunk(it, size):
    it = iter(it)
    return iter(lambda: tuple(islice(it, size)), ())


def load_model_from_config(config, ckpt, device=torch.device("cuda"), verbose=False):
    print(f"Loading model from {ckpt}")
    pl_sd = torch.load(ckpt, map_location="cpu")
    if "global_step" in pl_sd:
        print(f"Global Step: {pl_sd['global_step']}")
    sd = pl_sd["state_dict"]
    model = instantiate_from_config(config.model)
    m, u = model.load_state_dict(sd, strict=False)
    if len(m) > 0 and verbose:
        print("missing keys:")
        print(m)
    if len(u) > 0 and verbose:
        print("unexpected keys:")
        print(u)

    if device == torch.device("cuda"):
        model.cuda()
    elif device == torch.device("cpu"):
        model.cpu()
        model.cond_stage_model.device = "cpu"
    else:
        raise ValueError(f"Incorrect device name. Received: {device}")
    model.eval()
    return model


def parse_args():
    parser = argparse.ArgumentParser()
    # parser.add_argument(
    #     "--prompt",
    #     type=str,
    #     nargs="?",
    #     default="a professional photograph of an astronaut riding a triceratops",
    #     help="the prompt to render"
    # )
    parser.add_argument(
        "--brain_checkpoint",
        type=str,
        default="/home/jacob/projects/DeepLearningFinalProject/zcheckpoints/attention-1.pt",
        help="Path to the brain embedder checkpoint",
    )
    parser.add_argument(
        "--data_path",
        type=str,
        default="/home/jacob/projects/DeepLearningFinalProject/data/processed_data/",
        help="Path to the processed data",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=0,  # 0, #510,
        help="Index of the brain scan to use",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        nargs="?",
        help="dir to write results to",
        default="outputs/attention-1-train-test/",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=50,
        help="number of ddim sampling steps",
    )
    parser.add_argument(
        "--plms",
        action="store_true",
        help="use plms sampling",
    )
    parser.add_argument(
        "--dpm",
        action="store_true",
        help="use DPM (2) sampler",
    )
    parser.add_argument(
        "--fixed_code",
        action="store_true",
        help="if enabled, uses the same starting code across all samples ",
    )
    parser.add_argument(
        "--ddim_eta",
        type=float,
        default=0.0,
        help="ddim eta (eta=0.0 corresponds to deterministic sampling",
    )
    parser.add_argument(
        "--n_iter",
        type=int,
        default=2,
        help="sample this often",
    )
    parser.add_argument(
        "--H",
        type=int,
        default=512,
        help="image height, in pixel space",
    )
    parser.add_argument(
        "--W",
        type=int,
        default=512,
        help="image width, in pixel space",
    )
    parser.add_argument(
        "--C",
        type=int,
        default=4,
        help="latent channels",
    )
    parser.add_argument(
        "--f",
        type=int,
        default=8,
        help="downsampling factor, most often 8 or 16",
    )
    parser.add_argument(
        "--n_samples",
        type=int,
        default=3,
        help="how many samples to produce for each given prompt. A.k.a batch size",
    )
    parser.add_argument(
        "--n_rows",
        type=int,
        default=0,
        help="rows in the grid (default: n_samples)",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=9.0,
        help="unconditional guidance scale: eps = eps(x, empty) + scale * (eps(x, cond) - eps(x, empty))",
    )
    parser.add_argument(
        "--config",
        type=str,
        default="configs/stable-diffusion/v2-inference.yaml",
        help="path to config which constructs model",
    )
    parser.add_argument(
        "--ckpt",
        type=str,
        help="path to checkpoint of model",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="the seed (for reproducible sampling)",
    )
    parser.add_argument(
        "--precision",
        type=str,
        help="evaluate at this precision",
        choices=["full", "autocast"],
        default="autocast",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="repeat each prompt in file this often",
    )
    parser.add_argument(
        "--device",
        type=str,
        help="Device on which Stable Diffusion will be run",
        choices=["cpu", "cuda"],
        default="cuda",
    )
    parser.add_argument(
        "--bf16",
        action="store_true",
        help="Use bfloat16",
    )
    opt = parser.parse_args()
    return opt


def load_bs_embedder(self):
    try:
        snapshot = fsspec.open(opt.brain_checkpoint)
        with snapshot as f:
            snapshot_data = torch.load(f, map_location="cpu")
    except FileNotFoundError:
        print("Snapshot not found")
        raise FileNotFoundError

    snapshot = Snapshot(**snapshot_data)
    embedder = BrainScanEmbedder(ModelConfig([8, 16, 32, 48, 77], -1, 8))
    embedder.load_state_dict(snapshot.model_state)
    return embedder.to(opt.device)


def main(opt):
    # seed_everything(opt.seed)
    print("loading brain scan embedder")
    embedder = load_bs_embedder(opt)


    config = OmegaConf.load(f"{opt.config}")
    device = torch.device("cuda") if opt.device == "cuda" else torch.device("cpu")
    model = load_model_from_config(config, f"{opt.ckpt}", device)

    if opt.plms:
        sampler = PLMSSampler(model, device=device)
    elif opt.dpm:
        sampler = DPMSolverSampler(model, device=device)
    else:
        sampler = DDIMSampler(model, device=device)

    os.makedirs(opt.outdir, exist_ok=True)
    outpath = opt.outdir

    # print("Creating invisible watermark encoder (see https://github.com/ShieldMnt/invisible-watermark)...")
    # wm = "SDV2"
    # wm_encoder = WatermarkEncoder()
    # wm_encoder.set_watermark('bytes', wm.encode('utf-8'))

    batch_size = opt.n_samples
    n_rows = opt.n_rows if opt.n_rows > 0 else batch_size

    # prompt = opt.prompt
    # assert prompt is not None
    # data = [batch_size * [prompt]]

    sample_path = os.path.join(outpath, "samples")
    os.makedirs(sample_path, exist_ok=True)
    sample_count = 0
    base_count = len(os.listdir(sample_path))
    grid_count = len(os.listdir(outpath)) - 1

    start_code = None
    if opt.fixed_code:
        start_code = torch.randn(
            [opt.n_samples, opt.C, opt.H // opt.f, opt.W // opt.f], device=device
        )

    precision_scope = (
        autocast if opt.precision == "autocast" or opt.bf16 else nullcontext
    )
    print(model.device)

    # indexs =  [
    #         0,
    #         # 1,
    #         # 2,
    #         # 3,
    #         # 4,
    #         # 5,
    #         # 65,
    #         # 22557,
    #         # 6507,
    #         # 15218,
    #         # 13105,
    #         # 3416,
    #     ]
    
    data_json = os.path.join(opt.data_path, "dataset.json")
    data = json.load(open(data_json))

    with torch.no_grad(), precision_scope(opt.device), model.ema_scope():
        for i in [opt.index]:
            
            all_samples = list()
            new_save_path = os.path.join(outpath, f"results_image_{i}")
            os.makedirs(new_save_path, exist_ok=True)
            annotation = data["annotations"][i]
            beta_path = annotation["beta"]
            image_info = data["images"][str(annotation["img"])]
            print(image_info)
            target_embed = image_info['captions'][0]['embd']
            target_embed = np.load(os.path.join(opt.data_path, target_embed))
            
            uc = None
            if opt.scale != 1.0:
                # assert False
                uc = model.get_learned_conditioning(batch_size * [""])
            
            shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
            samples, _ = sampler.sample(
                S=opt.steps,
                conditioning=torch.from_numpy(target_embed).to(opt.device).unsqueeze(0),
                batch_size=1,
                shape=shape,
                verbose=False,
                unconditional_guidance_scale=opt.scale,
                unconditional_conditioning=uc,
                eta=opt.ddim_eta,
                x_T=start_code,
            )

            x_samples = model.decode_first_stage(samples)
            x_sample = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)[0]

            
            x_sample = 255.0 * rearrange(
                x_sample.cpu().numpy(), "c h w -> h w c"
            )
            img = Image.fromarray(x_sample.astype(np.uint8))
            img.save(os.path.join(new_save_path, f"target_generation.png"))         
            
                        
            c = (
                torch.from_numpy(
                    np.load(os.path.join(opt.data_path, beta_path)).astype(
                        np.float32
                    )
                )
                .to(opt.device)
                .unsqueeze(0)
            )

            c = embedder(c)

            im = Image.open(os.path.join(opt.data_path, image_info["im_path"]))
            im.save(os.path.join(new_save_path, f"target_{i}.png"))
            im_num = 0
            for n in trange(opt.n_iter, desc="Sampling"):
                uc = None
                if opt.scale != 1.0:
                    # assert False
                    uc = model.get_learned_conditioning(batch_size * [""])

                # Get the conditioning from the brain scan



                shape = [opt.C, opt.H // opt.f, opt.W // opt.f]
                samples, _ = sampler.sample(
                    S=opt.steps,
                    conditioning=c,
                    batch_size=opt.n_samples,
                    shape=shape,
                    verbose=False,
                    unconditional_guidance_scale=opt.scale,
                    unconditional_conditioning=uc,
                    eta=opt.ddim_eta,
                    x_T=start_code,
                )

                x_samples = model.decode_first_stage(samples)
                x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)

                for i, x_sample in enumerate(x_samples):
                    x_sample = 255.0 * rearrange(
                        x_sample.cpu().numpy(), "c h w -> h w c"
                    )
                    img = Image.fromarray(x_sample.astype(np.uint8))
                    # img = put_watermark(img, wm_encoder)
                    img.save(os.path.join(new_save_path, f"img_{im_num}.png"))
                    base_count += 1
                    sample_count += 1
                    im_num += 1

                all_samples.append(x_samples)

            # additionally, save as grid
            grid = torch.stack(all_samples, 0)
            grid = rearrange(grid, "n b c h w -> (n b) c h w")
            grid = make_grid(grid, nrow=n_rows)

            # to image
            grid = 255.0 * rearrange(grid, "c h w -> h w c").cpu().numpy()
            grid = Image.fromarray(grid.astype(np.uint8))
            # grid = put_watermark(grid, wm_encoder)
            grid.save(os.path.join(new_save_path, f"grid-{i}.png"))
            grid_count += 1

    print(
        f"Your samples are ready and waiting for you here: \n{outpath} \n" f" \nEnjoy."
    )


if __name__ == "__main__":
    opt = parse_args()
    main(opt)
