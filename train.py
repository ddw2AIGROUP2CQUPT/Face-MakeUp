import os
import random
import argparse
from pathlib import Path
import json
import itertools
import time
from packaging import version
import torch
import torch.nn.functional as F
from torchvision import transforms
from PIL import Image
from transformers import CLIPImageProcessor
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration
from diffusers import AutoencoderKL, DDPMScheduler, UNet2DConditionModel
from transformers import CLIPTextModel, CLIPTokenizer, CLIPVisionModelWithProjection

from facemakeup.utils import is_torch2_available
from facemakeup.attention_processor_faceid import LoRAAttnProcessor, LoRAIPAttnProcessor

from diffusers.models import ControlNetModel
from diffusers.utils.import_utils import is_xformers_available
from FaceMakeUp.facemakeup.facemakeup import ProjPlusModel

# Dataset
class MyDataset(torch.utils.data.Dataset):

    def __init__(self, json_file, tokenizer, size=512, t_drop_rate=0.05, i_drop_rate=0.05, ti_drop_rate=0.05, image_root_path="", face_id_dir="", control_img_dir=""):
        super().__init__()

        self.tokenizer = tokenizer
        self.size = size
        self.i_drop_rate = i_drop_rate
        self.t_drop_rate = t_drop_rate
        self.ti_drop_rate = ti_drop_rate
        self.image_root_path = image_root_path
        self.face_id_dir = face_id_dir
        self.control_img_dir = control_img_dir

        self.data = json.load(open(json_file)) 
        
        self.clip_image_processor = CLIPImageProcessor()
        
        

        data_ = []
        for item in self.data:
            image_file = item["face"][0]
            face_id_file = image_file.replace(".jpg", ".pt")
            
            control_img_file = item['image']


            if os.path.exists(os.path.join(face_id_dir, face_id_file)) and os.path.exists(os.path.join(control_img_dir, control_img_file)):
                data_.append(item)
     

        self.data = data_
        print(f"{len(self.data)} images loaded.")
        self.transform = transforms.Compose([
            transforms.Resize(self.size, interpolation=transforms.InterpolationMode.BILINEAR),
            transforms.CenterCrop(self.size),
            transforms.ToTensor(),
            transforms.Normalize([0.5], [0.5]),
        ])

        self.condition_transform = transforms.Compose([
            transforms.Resize(self.size, interpolation=transforms.InterpolationMode.BILINEAR),
            transforms.CenterCrop(self.size),
            transforms.ToTensor(),
        ])

        
        
    def __getitem__(self, idx):
        item = self.data[idx] 
        text = item['face_caption'][0]
        image_file = item["image"]
        
        face_id_embed_file = item["face"][0].replace(".jpg", ".pt")

        # read image
        raw_image = Image.open(os.path.join(self.image_root_path, image_file))
        
        face_image = Image.open(os.path.join("/home/ddwgroup/public-Datasets/HQFaceSquare400W/HQFaceSquare400W_FaceAlign", item["face"][0]))     
        
        contorlnet_image = Image.open(os.path.join(self.control_img_dir, image_file))

        contorlnet_image = self.condition_transform(contorlnet_image)

        image = self.transform(raw_image.convert("RGB"))

        clip_image_embed = self.clip_image_processor(images=face_image, return_tensors="pt")['pixel_values']

        try:
            face_id_embed = torch.load(os.path.join(self.face_id_dir, face_id_embed_file), map_location="cpu")
        except:
            face_id_embed = torch.zeros((1, 512))
        # drop 0.5 对齐clip embedding
        drop_image_embed = 0
        rand_num = random.randint(0, 9)
        if rand_num < 5:
            drop_image_embed = 1

        # 放弃丢弃
        if drop_image_embed:
            clip_image_embed = torch.zeros_like(clip_image_embed)


        # get text and tokenize
        text_input_ids = self.tokenizer(
            text,
            max_length=self.tokenizer.model_max_length,
            padding="max_length",
            truncation=True,
            return_tensors="pt"
        ).input_ids
        
        return {
            "image": image,
            "contorlnet_image": contorlnet_image,
            "text_input_ids": text_input_ids,
            "face_id_embed": face_id_embed,
            "clip_image_embed": clip_image_embed,
            "drop_image_embed": drop_image_embed
        }

    def __len__(self):
        return len(self.data)
    

def collate_fn(data):
    images = torch.stack([example["image"] for example in data])
    contorlnet_images = torch.stack([example["contorlnet_image"] for example in data])

    text_input_ids = torch.cat([example["text_input_ids"] for example in data], dim=0)
    face_id_embed = torch.stack([example["face_id_embed"] for example in data])
    drop_image_embeds = [example["drop_image_embed"] for example in data]

    clip_image_embed = torch.cat([example["clip_image_embed"] for example in data], dim=0)
    return {
        "images": images,
        "text_input_ids": text_input_ids,
        "contorlnet_image": contorlnet_images,
        "face_id_embed": face_id_embed,
        "clip_image_embed": clip_image_embed,
        "drop_image_embeds": drop_image_embeds
    }
    


class FaceMakeUp(torch.nn.Module):

    def __init__(self, unet, image_proj_model, adapter_modules, contorlnet, ckpt_path=None):
        super().__init__()
        self.unet = unet
        self.image_proj_model = image_proj_model
        self.adapter_modules = adapter_modules
        self.contorlnet = contorlnet

        if ckpt_path is not None:
            self.load_from_checkpoint(ckpt_path)

    def forward(self, noisy_latents, timesteps, encoder_hidden_states, face_id_embeds, image_embeds, controlnet_image):

        
        ip_tokens = self.image_proj_model(face_id_embeds, image_embeds, shortcut=True)
        encoder_hidden_states = torch.cat([encoder_hidden_states, ip_tokens], dim=1)
        
        down_block_res_samples, mid_block_res_sample = self.contorlnet(
            noisy_latents,
            timesteps,
            encoder_hidden_states=ip_tokens,
            controlnet_cond=controlnet_image,
            return_dict=False,
        )

        
        # Predict the noise residual
        
        noise_pred = self.unet(
            noisy_latents, 
            timesteps,
            encoder_hidden_states = encoder_hidden_states,
            down_block_additional_residuals=[
                        sample.to(dtype=encoder_hidden_states.dtype) for sample in down_block_res_samples
                    ],
            mid_block_additional_residual=mid_block_res_sample.to(dtype=encoder_hidden_states.dtype),
        ).sample
        return noise_pred

    def load_from_checkpoint(self, ckpt_path: str):
        # Calculate original checksums
        orig_ip_proj_sum = torch.sum(torch.stack([torch.sum(p) for p in self.image_proj_model.parameters()]))
        orig_adapter_sum = torch.sum(torch.stack([torch.sum(p) for p in self.adapter_modules.parameters()]))

        state_dict = torch.load(ckpt_path, map_location="cpu")

        # Load state dict for image_proj_model and adapter_modules
        self.image_proj_model.load_state_dict(state_dict["image_proj"], strict=True)
        self.adapter_modules.load_state_dict(state_dict["ip_adapter"], strict=True)

        # Calculate new checksums
        new_ip_proj_sum = torch.sum(torch.stack([torch.sum(p) for p in self.image_proj_model.parameters()]))
        new_adapter_sum = torch.sum(torch.stack([torch.sum(p) for p in self.adapter_modules.parameters()]))

        # Verify if the weights have changed
        assert orig_ip_proj_sum != new_ip_proj_sum, "Weights of image_proj_model did not change!"
        assert orig_adapter_sum != new_adapter_sum, "Weights of adapter_modules did not change!"

        print(f"Successfully loaded weights from checkpoint {ckpt_path}")


    
def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--pretrained_model_name_or_path",
        type=str,
        default=None,
        required=True,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
    )
    parser.add_argument(
        "--pretrained_facemakeup_path",
        type=str,
        default=None,
        help="Path to pretrained ip adapter model. If not specified weights are initialized randomly.",
    )
    parser.add_argument(
        "--data_json_file",
        type=str,
        default=None,
        required=True,
        help="Training data",
    )
    parser.add_argument(
        "--data_root_path",
        type=str,
        default="",
        required=True,
        help="Training data root path",
    )
    parser.add_argument(
        "--face_id_dir",
        type=str,
        default="/home/ddwgroup/workplace/hq1M_face_square_faceidEmbed",
        required=True,
        help="face_id_embed",
    )
    parser.add_argument(
        "--control_img_dir",
        type=str,
        default="/home/ddwgroup/public-Datasets/HQFaceSquare400W/HQFaceSquare400W_KPS",
        required=True,
        help="control img dir",
    )
    parser.add_argument(
        "--image_encoder_path",
        type=str,
        default=None,
        required=True,
        help="Path to CLIP image encoder",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="sd-ip_adapter",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument(
        "--enable_xformers_memory_efficient_attention",
        action="store_true",
        help="Enable xformers",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=512,
        help=(
            "The resolution for input images"
        ),
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Learning rate to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=1e-2, help="Weight decay to use.")
    parser.add_argument("--num_train_epochs", type=int, default=100)
    parser.add_argument(
        "--train_batch_size", type=int, default=8, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=0,
        help=(
            "Number of subprocesses to use for data loading. 0 means that the data will be loaded in the main process."
        ),
    )
    parser.add_argument(
        "--save_steps",
        type=int,
        default=2000,
        help=(
            "Save a checkpoint of the training state every X updates"
        ),
    )
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default=None,
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >="
            " 1.10.and an Nvidia Ampere GPU.  Default to the value of accelerate config of the current system or the"
            " flag passed with the `accelerate.launch` command. Use this argument to override the accelerate config."
        ),
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`'
            ' (default), `"wandb"` and `"comet_ml"`. Use `"all"` to report to all integrations.'
        ),
    )
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    
    args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    return args
    

def main():
    args = parse_args()
    logging_dir = Path(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(project_dir=args.output_dir, logging_dir=logging_dir)

    accelerator = Accelerator(
        mixed_precision=args.mixed_precision,
        log_with=args.report_to,
        project_config=accelerator_project_config,
    )
    
    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # Load scheduler, tokenizer and models.
    noise_scheduler = DDPMScheduler.from_pretrained(args.pretrained_model_name_or_path, subfolder="scheduler")
    tokenizer = CLIPTokenizer.from_pretrained(args.pretrained_model_name_or_path, subfolder="tokenizer")
    text_encoder = CLIPTextModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="text_encoder")
    vae = AutoencoderKL.from_pretrained(args.pretrained_model_name_or_path, subfolder="vae")
    unet = UNet2DConditionModel.from_pretrained(args.pretrained_model_name_or_path, subfolder="unet")
    image_encoder = CLIPVisionModelWithProjection.from_pretrained(args.image_encoder_path)

    # 从预训练的控制网络中加载模型（第二次训练）
    # ControlNetModel.from_pretrained(args.controlnet_path)
    # 从Unet中加载控制网络（默认，初始
    controlnet = ControlNetModel.from_unet(unet)


    # freeze parameters of models to save more memory
    unet.requires_grad_(False)
    vae.requires_grad_(False)
    controlnet.train()
    text_encoder.requires_grad_(False)
    image_encoder.requires_grad_(False)
    
    if args.enable_xformers_memory_efficient_attention:
        if is_xformers_available():
            import xformers

            xformers_version = version.parse(xformers.__version__)
            if xformers_version == version.parse("0.0.16"):
                print("xFormers 0.0.16 cannot be used for training in some GPUs. If you observe problems during training, please update xFormers to at least 0.0.17. See https://huggingface.co/docs/diffusers/main/en/optimization/xformers for more details")
            unet.enable_xformers_memory_efficient_attention()
            controlnet.enable_xformers_memory_efficient_attention()
        else:
            raise ValueError("xformers is not available. Make sure it is installed correctly")
        
 
    image_proj_model = ProjPlusModel(
        cross_attention_dim=unet.config.cross_attention_dim,
        id_embeddings_dim=512,
        clip_embeddings_dim=image_encoder.config.hidden_size,
        num_tokens=4
    )
    # init adapter modules
    lora_rank = 128
    attn_procs = {}
    unet_sd = unet.state_dict()
    for name in unet.attn_processors.keys():
        cross_attention_dim = None if name.endswith("attn1.processor") else unet.config.cross_attention_dim
        if name.startswith("mid_block"):
            hidden_size = unet.config.block_out_channels[-1]
        elif name.startswith("up_blocks"):
            block_id = int(name[len("up_blocks.")])
            hidden_size = list(reversed(unet.config.block_out_channels))[block_id]
        elif name.startswith("down_blocks"):
            block_id = int(name[len("down_blocks.")])
            hidden_size = unet.config.block_out_channels[block_id]
        if cross_attention_dim is None:
            attn_procs[name] = LoRAAttnProcessor(hidden_size=hidden_size, cross_attention_dim=cross_attention_dim, rank=lora_rank)
        else:
            layer_name = name.split(".processor")[0]
            weights = {
                "to_k_ip.weight": unet_sd[layer_name + ".to_k.weight"],
                "to_v_ip.weight": unet_sd[layer_name + ".to_v.weight"],
            }
            attn_procs[name] = LoRAIPAttnProcessor(hidden_size=hidden_size, cross_attention_dim=cross_attention_dim, rank=lora_rank)
            attn_procs[name].load_state_dict(weights, strict=False)
    unet.set_attn_processor(attn_procs)
    adapter_modules = torch.nn.ModuleList(unet.attn_processors.values())
    


    faceMakeUp = FaceMakeUp(unet, image_proj_model, adapter_modules, controlnet, args.pretrained_facemakeup_path)
    
    weight_dtype = torch.float32
    if accelerator.mixed_precision == "fp16":
        weight_dtype = torch.float16
    elif accelerator.mixed_precision == "bf16":
        weight_dtype = torch.bfloat16
    unet.to(accelerator.device, dtype=weight_dtype)
    vae.to(accelerator.device, dtype=weight_dtype)
    text_encoder.to(accelerator.device, dtype=weight_dtype)
    image_encoder.to(accelerator.device, dtype=weight_dtype)
    
    # optimizer
    params_to_opt = itertools.chain(faceMakeUp.image_proj_model.parameters(),  faceMakeUp.adapter_modules.parameters(), faceMakeUp.contorlnet.parameters())
    optimizer = torch.optim.AdamW(params_to_opt, lr=args.learning_rate, weight_decay=args.weight_decay)
    
    # dataloader
    train_dataset = MyDataset(args.data_json_file, tokenizer=tokenizer, size=args.resolution, image_root_path=args.data_root_path, face_id_dir=args.face_id_dir, control_img_dir=args.control_img_dir)
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=collate_fn,
        batch_size=args.train_batch_size,
        num_workers=args.dataloader_num_workers,
    )
    
    # Prepare everything with our `accelerator`.
    faceMakeUp, optimizer, train_dataloader = accelerator.prepare(faceMakeUp, optimizer, train_dataloader)
    
    global_step = 0
    for epoch in range(0, args.num_train_epochs):
        begin = time.perf_counter()
        for step, batch in enumerate(train_dataloader):
            load_data_time = time.perf_counter() - begin
            with accelerator.accumulate(faceMakeUp):
                # Convert images to latent space
                with torch.no_grad():
                    latents = vae.encode(batch["images"].to(accelerator.device, dtype=weight_dtype)).latent_dist.sample()
                    latents = latents * vae.config.scaling_factor

                # Sample noise that we'll add to the latents
                noise = torch.randn_like(latents)
                bsz = latents.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(0, noise_scheduler.num_train_timesteps, (bsz,), device=latents.device)
                timesteps = timesteps.long()

                # Add noise to the latents according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_latents = noise_scheduler.add_noise(latents, noise, timesteps).to(accelerator.device, dtype=weight_dtype)
                
                faceid_embeds = batch["face_id_embed"].to(accelerator.device, dtype=weight_dtype)

                clip_image_embeds = image_encoder(batch["clip_image_embed"].to(accelerator.device, dtype=weight_dtype) , output_hidden_states=True).hidden_states[-2]


                with torch.no_grad():
                    encoder_hidden_states = text_encoder(batch["text_input_ids"].to(accelerator.device))[0]


                controlnet_image = batch["contorlnet_image"].to(accelerator.device, dtype=weight_dtype)

                noise_pred = faceMakeUp(noisy_latents, timesteps, encoder_hidden_states, faceid_embeds, clip_image_embeds, controlnet_image)
        
                loss = F.mse_loss(noise_pred.float(), noise.float(), reduction="mean")
                # Gather the losses across all processes for logging (if we use distributed training).
                avg_loss = accelerator.gather(loss.repeat(args.train_batch_size)).mean().item()
                
                # Backpropagate
                accelerator.backward(loss)
                optimizer.step()
                optimizer.zero_grad()

                if accelerator.is_main_process:
                    print("Epoch {}, step {}, data_time: {}, time: {}, step_loss: {}".format(
                        epoch, step, load_data_time, time.perf_counter() - begin, avg_loss))
            
            global_step += 1
            
            if global_step % args.save_steps == 0:
                save_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")
                accelerator.save_state(save_path)
            
            begin = time.perf_counter()
                
if __name__ == "__main__":
    main()    
