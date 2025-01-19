import base64
import concurrent.futures
import dataclasses
import functools
import logging
import math
import os
import pathlib
import time
import typing

import beartype
import einops.layers.torch
import gradio as gr
import numpy as np
import open_clip
import PIL.Image
import pyvips
import torch
import torchvision.datasets
from jaxtyping import Float, Int, jaxtyped
from torch import Tensor

import saev.activations
import saev.nn

log_format = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=log_format)
logger = logging.getLogger("app.py")
# Disable pyvips info logging
logging.getLogger("pyvips").setLevel(logging.WARNING)


###########
# Globals #
###########


RESIZE_SIZE = (512, 512)

CROP_SIZE = (448, 448)

CROP_COORDS = (
    (RESIZE_SIZE[0] - CROP_SIZE[0]) // 2,
    (RESIZE_SIZE[1] - CROP_SIZE[1]) // 2,
    (RESIZE_SIZE[0] + CROP_SIZE[0]) // 2,
    (RESIZE_SIZE[1] + CROP_SIZE[1]) // 2,
)

DEBUG = True
"""Whether we are debugging."""

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
"""Hardware accelerator, if any."""

CWD = pathlib.Path(".")


@beartype.beartype
@dataclasses.dataclass(frozen=True)
class ModelConfig:
    """Configuration for a Vision Transformer (ViT) and Sparse Autoencoder (SAE) model pair.

    Stores paths and configuration needed to load and run a specific ViT+SAE combination.
    """

    vit_family: str
    """The family of ViT model, e.g. 'clip' for CLIP models."""

    vit_ckpt: str
    """Checkpoint identifier for the ViT model, either as HuggingFace path or model/checkpoint pair."""

    sae_ckpt: str
    """Identifier for the SAE checkpoint to load."""

    tensor_dpath: pathlib.Path
    """Directory containing precomputed tensors for this model combination."""

    dataset_name: str
    """Which dataset to use."""


MODEL_LOOKUP: dict[str, ModelConfig] = {
    "bioclip/inat21": ModelConfig(
        "clip",
        "hf-hub:imageomics/bioclip",
        "gpnn7x3p",
        pathlib.Path(
            "/research/nfs_su_809/workspace/stevens.994/saev/features/gpnn7x3p-high-freq/sort_by_patch/"
        ),
        "inat21__train_mini",
    ),
    "clip/inat21": ModelConfig(
        "clip",
        "ViT-B-16/openai",
        "rscsjxgd",
        pathlib.Path(
            "/research/nfs_su_809/workspace/stevens.994/saev/features/rscsjxgd-high-freq/sort_by_patch/"
        ),
        "inat21__train_mini",
    ),
}


logger.info("Set global constants.")

###########
# Helpers #
###########


@beartype.beartype
def get_cache_dir() -> str:
    """
    Get cache directory from environment variables, defaulting to the current working directory (.)

    Returns:
        A path to a cache directory (might not exist yet).
    """
    cache_dir = ""
    for var in ("HF_HOME", "HF_HUB_CACHE"):
        cache_dir = cache_dir or os.environ.get(var, "")
    return cache_dir or CWD


class VipsImageFolder(torchvision.datasets.ImageFolder):
    """
    Clone of ImageFolder that returns pyvips.Image instead of PIL.Image.Image.
    """

    def __init__(
        self,
        root: str,
        transform: typing.Callable | None = None,
        target_transform: typing.Callable | None = None,
    ):
        super().__init__(
            root,
            transform=transform,
            target_transform=target_transform,
            loader=self._vips_loader,
        )

    @staticmethod
    def _vips_loader(path: str) -> torch.Tensor:
        """Load and convert image to tensor using pyvips."""
        image = pyvips.Image.new_from_file(path, access="random")
        return image


datasets = {
    "inat21__train_mini": VipsImageFolder(
        root="/research/nfs_su_809/workspace/stevens.994/datasets/inat21/train_mini/"
    )
}


@beartype.beartype
def get_dataset_image(key: str, i: int) -> tuple[pyvips.Image, str]:
    """
    Get raw image and processed label from dataset.

    Returns:
        Tuple of pyvips.Image and classname.
    """
    dataset = datasets[key]
    img, tgt = dataset[i]
    species_label = dataset.classes[tgt]
    # iNat21 specific: Remove taxonomy prefix
    species_name = " ".join(species_label.split("_")[1:])
    return img, species_name


##########
# Models #
##########


@jaxtyped(typechecker=beartype.beartype)
class SplitClip(torch.nn.Module):
    def __init__(self, vit_ckpt: str, *, n_end_layers: int):
        super().__init__()

        if vit_ckpt.startswith("hf-hub:"):
            clip, _ = open_clip.create_model_from_pretrained(
                vit_ckpt, cache_dir=get_cache_dir()
            )
        else:
            arch, ckpt = vit_ckpt.split("/")
            clip, _ = open_clip.create_model_from_pretrained(
                arch, pretrained=ckpt, cache_dir=get_cache_dir()
            )
        model = clip.visual
        model.proj = None
        model.output_tokens = True  # type: ignore
        self.vit = model.eval()
        assert not isinstance(self.vit, open_clip.timm_model.TimmModel)

        self.n_end_layers = n_end_layers

    @staticmethod
    def _expand_token(token, batch_size: int):
        return token.view(1, 1, -1).expand(batch_size, -1, -1)

    def forward_start(self, x: Float[Tensor, "batch channels width height"]):
        x = self.vit.conv1(x)  # shape = [*, width, grid, grid]
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]

        # class embeddings and positional embeddings
        x = torch.cat(
            [self._expand_token(self.vit.class_embedding, x.shape[0]).to(x.dtype), x],
            dim=1,
        )
        # shape = [*, grid ** 2 + 1, width]
        x = x + self.vit.positional_embedding.to(x.dtype)

        x = self.vit.patch_dropout(x)
        x = self.vit.ln_pre(x)
        for r in self.vit.transformer.resblocks[: -self.n_end_layers]:
            x = r(x)
        return x

    def forward_end(self, x: Float[Tensor, "batch n_patches dim"]):
        for r in self.vit.transformer.resblocks[-self.n_end_layers :]:
            x = r(x)

        x = self.vit.ln_post(x)
        pooled, _ = self.vit._global_pool(x)
        if self.vit.proj is not None:
            pooled = pooled @ self.vit.proj

        return pooled


@beartype.beartype
@functools.cache
def load_split_vit(model_name: str) -> tuple[SplitClip, object]:
    # Translate model key to ckpt. Use the model as the default.
    model_cfg = MODEL_LOOKUP[model_name]
    split_vit = SplitClip(model_cfg.vit_ckpt, n_end_layers=1).to(DEVICE).eval()
    vit_transform = saev.activations.make_img_transform(
        model_cfg.vit_family, model_cfg.vit_ckpt
    )
    logger.info("Loaded Split ViT: %s.", model_name)
    return split_vit, vit_transform


@beartype.beartype
@functools.cache
def load_sae(model_name: str) -> saev.nn.SparseAutoencoder:
    model_cfg = MODEL_LOOKUP[model_name]
    sae_ckpt_fpath = CWD / "checkpoints" / model_cfg.sae_ckpt / "sae.pt"
    sae = saev.nn.load(sae_ckpt_fpath.as_posix())
    sae.to(DEVICE).eval()
    logger.info("Loaded SAE: %s -> %s.", model_name, model_cfg.sae_ckpt)
    return sae


@beartype.beartype
def load_tensor(path: str | pathlib.Path) -> Tensor:
    return torch.load(path, weights_only=True, map_location="cpu")


@beartype.beartype
@functools.cache
def load_tensors(
    model_name: str,
) -> tuple[Int[Tensor, "d_sae top_k"], Float[Tensor, "d_sae top_k n_patches"]]:
    model_cfg = MODEL_LOOKUP[model_name]
    top_img_i = load_tensor(model_cfg.tensor_dpath / "top_img_i.pt")
    top_values = load_tensor(model_cfg.tensor_dpath / "top_values.pt")
    return top_img_i, top_values


############
# Datasets #
############


def to_sized(img_v_raw: pyvips.Image) -> pyvips.Image:
    """Convert raw vips image to standard model input size (resize + crop)."""
    # Calculate scaling factors to reach RESIZE_SIZE
    hscale = RESIZE_SIZE[0] / img_v_raw.width
    vscale = RESIZE_SIZE[1] / img_v_raw.height

    # Resize then crop to CROP_COORDS
    resized = img_v_raw.resize(hscale, vscale=vscale)
    return resized.crop(*CROP_COORDS)


logger.info("Loaded all datasets.")


@beartype.beartype
def vips_to_base64(img_v: pyvips.Image) -> str:
    buf = img_v.write_to_buffer(".webp")
    b64 = base64.b64encode(buf)
    s64 = b64.decode("utf8")
    return "data:image/webp;base64," + s64


@beartype.beartype
def get_image(example_id: str) -> list[str]:
    dataset, split, i_str = example_id.split("__")
    i = int(i_str)
    img_v_raw, label = get_dataset_image(f"{dataset}__{split}", i)
    img_v_sized = to_sized(img_v_raw)

    return [vips_to_base64(img_v_sized), label]


@jaxtyped(typechecker=beartype.beartype)
def add_highlights(
    img_v_sized: pyvips.Image,
    patches: np.ndarray,
    *,
    upper: float | None = None,
    opacity: float = 0.9,
) -> pyvips.Image:
    """Add colored highlights to an image based on patch activation values.

    Overlays a colored highlight on each patch of the image, with intensity proportional
    to the activation value for that patch. Used to visualize which parts of an image
    most strongly activated a particular SAE latent.

    Args:
        img: The base image to highlight
        patches: Array of activation values, one per patch
        upper: Optional maximum value to normalize activations against
        opacity: Opacity of the highlight overlay (0-1)

    Returns:
        A new image with colored highlights overlaid on the original
    """
    if not len(patches):
        return img_v_sized

    # Calculate patch grid dimensions
    grid_w = grid_h = int(math.sqrt(len(patches)))
    assert grid_w * grid_h == len(patches)

    patch_w = img_v_sized.width // grid_w
    patch_h = img_v_sized.height // grid_h
    assert patch_w == patch_h

    # Create overlay by processing each patch
    overlay = np.zeros((img_v_sized.width, img_v_sized.height, 4), dtype=np.uint8)
    for idx, val in enumerate(patches):
        assert upper is not None
        val = val / (upper + 1e-9)

        x = (idx % grid_w) * patch_w
        y = (idx // grid_w) * patch_h

        # Create patch overlay
        patch = np.zeros((patch_w, patch_h, 4), dtype=np.uint8)
        patch[:, :, 0] = int(255 * val)
        patch[:, :, 3] = int(128 * val)
        overlay[y : y + patch_h, x : x + patch_w, :] = patch
    overlay = pyvips.Image.new_from_array(overlay).copy(interpretation="srgb")
    return img_v_sized.addalpha().composite(overlay, "over")


@beartype.beartype
class Example(typing.TypedDict):
    """Represents an example image and its associated label.

    Used to store examples of SAE latent activations for visualization.
    """

    orig_url: str
    """The URL or path to access the original example image."""
    highlighted_url: str
    """The URL or path to access the SAE-highlighted image."""
    label: str
    """The class label or description associated with this example."""
    example_id: str
    """Unique ID to idenfify the original dataset instance."""


@beartype.beartype
class SaeActivation(typing.TypedDict):
    """Represents the activation pattern of a single SAE latent across patches.

    This captures how strongly a particular SAE latent fires on different patches of an input image.
    """

    model_name: str
    """The model key."""

    latent: int
    """The index of the SAE latent being measured."""

    activations: list[float]
    """The activation values of this latent across different patches. Each value represents how strongly this latent fired on a particular patch."""

    examples: list[Example]
    """Top examples for this latent."""


@beartype.beartype
def pil_to_vips(pil_img: PIL.Image.Image) -> pyvips.Image:
    # Convert to numpy array
    np_array = np.asarray(pil_img)
    # Handle different formats
    if np_array.ndim == 2:  # Grayscale
        return pyvips.Image.new_from_memory(
            np_array.tobytes(),
            np_array.shape[1],  # width
            np_array.shape[0],  # height
            1,  # bands
            "uchar",
        )
    else:  # RGB/RGBA
        return pyvips.Image.new_from_memory(
            np_array.tobytes(),
            np_array.shape[1],  # width
            np_array.shape[0],  # height
            np_array.shape[2],  # bands
            "uchar",
        )


@beartype.beartype
def vips_to_pil(vips_img: PIL.Image.Image) -> PIL.Image.Image:
    # Convert to numpy array
    np_array = vips_img.numpy()
    # Convert numpy array to PIL Image
    return PIL.Image.fromarray(np_array)


@beartype.beartype
class BufferInfo(typing.NamedTuple):
    buffer: bytes
    width: int
    height: int
    bands: int
    format: object

    @classmethod
    def from_img_v(cls, img_v: pyvips.Image) -> "BufferInfo":
        return cls(
            img_v.write_to_memory(),
            img_v.width,
            img_v.height,
            img_v.bands,
            img_v.format,
        )


@beartype.beartype
def bufferinfo_to_base64(bufferinfo: BufferInfo) -> str:
    img_v = pyvips.Image.new_from_memory(*bufferinfo)
    buf = img_v.write_to_buffer(".webp")
    b64 = base64.b64encode(buf)
    s64 = b64.decode("utf8")
    return "data:image/webp;base64," + s64


@jaxtyped(typechecker=beartype.beartype)
def make_sae_activation(
    model_name: str,
    latent: int,
    acts: list[float],
    top_img_i: list[int],
    top_values: Float[Tensor, "top_k n_patches"],
    pool: concurrent.futures.Executor,
) -> SaeActivation:
    dataset_name = MODEL_LOOKUP[model_name].dataset_name
    raw_examples: list[tuple[int, pyvips.Image, Float[np.ndarray, "..."], str]] = []
    seen_i_im = set()
    for i_im, values_p in zip(top_img_i, top_values):
        if i_im in seen_i_im:
            continue

        ex_img_v_raw, ex_label = get_dataset_image(dataset_name, i_im)
        ex_img_v_sized = to_sized(ex_img_v_raw)
        raw_examples.append((i_im, ex_img_v_sized, values_p.numpy(), ex_label))

        seen_i_im.add(i_im)

        # Only need 4 example images per latent.
        if len(seen_i_im) >= 4:
            break

    upper = top_values.max().item()

    futures = []
    for i_im, ex_img, values_p, ex_label in raw_examples:
        highlighted_img = add_highlights(ex_img, values_p, upper=upper)
        # Submit both conversions to the thread pool
        orig_future = pool.submit(vips_to_base64, ex_img)
        highlight_future = pool.submit(vips_to_base64, highlighted_img)
        futures.append((i_im, orig_future, highlight_future, ex_label))

    # Wait for all conversions to complete and build examples
    examples = []
    for i_im, orig_future, highlight_future, ex_label in futures:
        example = Example(
            orig_url=orig_future.result(),
            highlighted_url=highlight_future.result(),
            label=ex_label,
            example_id=f"inat21__train_mini__{i_im}",
        )
        examples.append(example)

    return SaeActivation(
        model_name=model_name, latent=latent, activations=acts, examples=examples
    )


@beartype.beartype
@torch.inference_mode
def get_sae_activations(
    img_p: PIL.Image.Image, latents: dict[str, list[int]]
) -> dict[str, list[SaeActivation]]:
    """
    Args:
        image: Image to get SAE activations for.
        latents: A lookup from model name (string) to a list of latents to report latents for (integers).

    Returns:
        A lookup from model name (string) to a list of SaeActivations, one for each latent in the `latents` argument.
    """
    response = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        for model_name, requested_latents in latents.items():
            sae_activations = []
            split_vit, vit_transform = load_split_vit(model_name)
            sae = load_sae(model_name)
            x = vit_transform(img_p)[None, ...].to(DEVICE)
            vit_acts_PD = split_vit.forward_start(x)[0]

            _, f_x_PS, _ = sae(vit_acts_PD)
            # Ignore [CLS] token and get just the requested latents.
            acts_SP = einops.rearrange(
                f_x_PS[1:], "patches n_latents -> n_latents patches"
            )
            logger.info("Got SAE activations for '%s'.", model_name)
            top_img_i, top_values = load_tensors(model_name)
            logger.info("Loaded top SAE activations for '%s'.", model_name)

            for latent in requested_latents:
                sae_activations.append(
                    make_sae_activation(
                        model_name,
                        latent,
                        acts_SP[latent].cpu().tolist(),
                        top_img_i[latent].tolist(),
                        top_values[latent],
                        pool,
                    )
                )
            response[model_name] = sae_activations
    return response


@beartype.beartype
class progress:
    def __init__(self, it, *, every: int = 10, desc: str = "progress", total: int = 0):
        """
        Wraps an iterable with a logger like tqdm but doesn't use any control codes to manipulate a progress bar, which doesn't work well when your output is redirected to a file. Instead, simple logging statements are used, but it includes quality-of-life features like iteration speed and predicted time to finish.

        Args:
            it: Iterable to wrap.
            every: How many iterations between logging progress.
            desc: What to name the logger.
            total: If non-zero, how long the iterable is.
        """
        self.it = it
        self.every = every
        self.logger = logging.getLogger(desc)
        self.total = total

    def __iter__(self):
        start = time.time()

        try:
            total = len(self)
        except TypeError:
            total = None

        for i, obj in enumerate(self.it):
            yield obj

            if (i + 1) % self.every == 0:
                now = time.time()
                duration_s = now - start
                per_min = (i + 1) / (duration_s / 60)

                if total is not None:
                    pred_min = (total - (i + 1)) / per_min
                    self.logger.info(
                        "%d/%d (%.1f%%) | %.1f it/m (expected finish in %.1fm)",
                        i + 1,
                        total,
                        (i + 1) / total * 100,
                        per_min,
                        pred_min,
                    )
                else:
                    self.logger.info("%d/? | %.1f it/m", i + 1, per_min)

    def __len__(self) -> int:
        if self.total > 0:
            return self.total

        # Will throw exception.
        return len(self.it)


#############
# Interface #
#############


with gr.Blocks() as demo:
    example_id_text = gr.Text(label="Test Example")
    input_image = gr.Image(
        label="Input Image",
        sources=["upload", "clipboard"],
        type="pil",
        interactive=True,
    )

    input_image_base64 = gr.Text(label="Image in Base64")
    input_image_label = gr.Text(label="Image Label")
    get_input_image_btn = gr.Button(value="Get Input Image")
    get_input_image_btn.click(
        get_image,
        inputs=[example_id_text],
        outputs=[input_image_base64, input_image_label],
        api_name="get-image",
        postprocess=False,
    )

    latents_json = gr.JSON(label="Latents", value={})
    activations_json = gr.JSON(label="Activations", value={})

    get_sae_activations_btn = gr.Button(value="Get SAE Activations")
    get_sae_activations_btn.click(
        get_sae_activations,
        inputs=[input_image, latents_json],
        outputs=[activations_json],
        api_name="get-sae-activations",
    )


if __name__ == "__main__":
    demo.launch()
