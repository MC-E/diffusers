# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import List, Optional

import torch
import torch.nn as nn

from ..configuration_utils import ConfigMixin, register_to_config
from .modeling_utils import ModelMixin
from .resnet import Downsample2D


class MultiAdapter(ModelMixin):
    r"""
    MultiAdapter is a wrapper model that contains multiple adapter models and merges their outputs according to
    user-assigned weighting.

    This model inherits from [`ModelMixin`]. Check the superclass documentation for the generic methods the library
    implements for all the model (such as downloading or saving, etc.)

    Parameters:
        adapters (`List[T2IAdapter]`, *optional*, defaults to None):
            A list of `T2IAdapter` model instances.
    """

    def __init__(self, adapters: List["T2IAdapter"]):
        super(MultiAdapter, self).__init__()

        self.num_adapter = len(adapters)
        self.adapters = nn.ModuleList(adapters)

        if len(adapters) == 0:
            raise ValueError("Expecting at least one adapter")

        if len(adapters) == 1:
            raise ValueError("For a single adapter, please use the `T2IAdapter` class instead of `MultiAdapter`")

        # The outputs from each adapter are added together with a weight
        # This means that the change in dimenstions from downsampling must
        # be the same for all adapters. Inductively, it also means the total
        # downscale factor must also be the same for all adapters.

        first_adapter_total_downscale_factor = adapters[0].total_downscale_factor

        for idx in range(1, len(adapters)):
            adapter_idx_total_downscale_factor = adapters[idx].total_downscale_factor

            if adapter_idx_total_downscale_factor != first_adapter_total_downscale_factor:
                raise ValueError(
                    f"Expecting all adapters to have the same total_downscale_factor, "
                    f"but got adapters[0].total_downscale_factor={first_adapter_total_downscale_factor} and "
                    f"adapter[`{idx}`]={adapter_idx_total_downscale_factor}"
                )

        self.total_downscale_factor = adapters[0].total_downscale_factor

    def forward(self, xs: torch.Tensor, adapter_weights: Optional[List[float]] = None) -> List[torch.Tensor]:
        r"""
        Args:
            xs (`torch.Tensor`):
                (batch, channel, height, width) input images for multiple adapter models concated along dimension 1,
                `channel` should equal to `num_adapter` * "number of channel of image".
            adapter_weights (`List[float]`, *optional*, defaults to None):
                List of floats representing the weight which will be multiply to each adapter's output before adding
                them together.
        """
        if adapter_weights is None:
            adapter_weights = torch.tensor([1 / self.num_adapter] * self.num_adapter)
        else:
            adapter_weights = torch.tensor(adapter_weights)

        accume_state = None
        for x, w, adapter in zip(xs, adapter_weights, self.adapters):
            features = adapter(x)
            if accume_state is None:
                accume_state = features
            else:
                for i in range(len(features)):
                    accume_state[i] += w * features[i]
        return accume_state


class T2IAdapter(ModelMixin, ConfigMixin):
    r"""
    A simple ResNet-like model that accepts images containing control signals such as keyposes and depth. The model
    generates multiple feature maps that are used as additional conditioning in [`UNet2DConditionModel`]. The model's
    architecture follows the original implementation of
    [Adapter](https://github.com/TencentARC/T2I-Adapter/blob/686de4681515662c0ac2ffa07bf5dda83af1038a/ldm/modules/encoders/adapter.py#L97)
     and
     [AdapterLight](https://github.com/TencentARC/T2I-Adapter/blob/686de4681515662c0ac2ffa07bf5dda83af1038a/ldm/modules/encoders/adapter.py#L235).

    This model inherits from [`ModelMixin`]. Check the superclass documentation for the generic methods the library
    implements for all the model (such as downloading or saving, etc.)

    Parameters:
        in_channels (`int`, *optional*, defaults to 3):
            Number of channels of Aapter's input(*control image*). Set this parameter to 1 if you're using gray scale
            image as *control image*.
        channels (`List[int]`, *optional*, defaults to `(320, 640, 1280, 1280)`):
            The number of channel of each downsample block's output hidden state. The `len(block_out_channels)` will
            also determine the number of downsample blocks in the Adapter.
        num_res_blocks (`int`, *optional*, defaults to 2):
            Number of ResNet blocks in each downsample block
    """

    @register_to_config
    def __init__(
        self,
        in_channels: int = 3,
        channels: List[int] = [320, 640, 1280, 1280],
        num_res_blocks: int = 2,
        downscale_factor: int = 8,
        adapter_type: str = "full_adapter",
    ):
        super().__init__()

        if adapter_type == "full_adapter":
            self.adapter = FullAdapter(in_channels, channels, num_res_blocks, downscale_factor)
        elif adapter_type == "full_adapter_xl":
            self.adapter = FullAdapterXL(in_channels, channels, num_res_blocks, downscale_factor)
        elif adapter_type == "light_adapter":
            self.adapter = LightAdapter(in_channels, channels, num_res_blocks, downscale_factor)
        else:
            raise ValueError(f"unknown adapter_type: {type}. Choose either 'full_adapter' or 'simple_adapter'")

    def forward(self, x: torch.Tensor) -> List[torch.Tensor]:
        return self.adapter(x)

    @property
    def total_downscale_factor(self):
        return self.adapter.total_downscale_factor


# full adapter


class FullAdapter(nn.Module):
    def __init__(
        self,
        in_channels: int = 3,
        channels: List[int] = [320, 640, 1280, 1280],
        num_res_blocks: int = 2,
        downscale_factor: int = 8,
    ):
        super().__init__()

        in_channels = in_channels * downscale_factor**2

        self.unshuffle = nn.PixelUnshuffle(downscale_factor)
        self.conv_in = nn.Conv2d(in_channels, channels[0], kernel_size=3, padding=1)

        self.body = nn.ModuleList(
            [
                AdapterBlock(channels[0], channels[0], num_res_blocks),
                *[
                    AdapterBlock(channels[i - 1], channels[i], num_res_blocks, down=True)
                    for i in range(1, len(channels))
                ],
            ]
        )

        self.total_downscale_factor = downscale_factor * 2 ** (len(channels) - 1)

    def forward(self, x: torch.Tensor) -> List[torch.Tensor]:
        x = self.unshuffle(x)
        x = self.conv_in(x)

        features = []

        for block in self.body:
            x = block(x)
            features.append(x)

        return features


class FullAdapterXL(nn.Module):
    def __init__(
        self,
        in_channels: int = 3,
        channels: List[int] = [320, 640, 1280, 1280],
        num_res_blocks: int = 2,
        downscale_factor: int = 16,
    ):
        super().__init__()

        in_channels = in_channels * downscale_factor**2

        self.unshuffle = nn.PixelUnshuffle(downscale_factor)
        self.conv_in = nn.Conv2d(in_channels, channels[0], kernel_size=3, padding=1)

        self.body = []
        # blocks to extract XL features with dimensions of [320, 64, 64], [640, 64, 64], [1280, 32, 32], [1280, 32, 32]
        for i in range(len(channels)):
            if i == 1:
                self.body.append(AdapterBlock(channels[i - 1], channels[i], num_res_blocks))
            elif i == 2:
                self.body.append(AdapterBlock(channels[i - 1], channels[i], num_res_blocks, down=True))
            else:
                self.body.append(AdapterBlock(channels[i], channels[i], num_res_blocks))

        self.body = nn.ModuleList(self.body)
        # XL has one fewer downsampling
        self.total_downscale_factor = downscale_factor * 2 ** (len(channels) - 2)

    def forward(self, x: torch.Tensor) -> List[torch.Tensor]:
        x = self.unshuffle(x)
        x = self.conv_in(x)

        features = []

        for block in self.body:
            x = block(x)
            features.append(x)

        return features


class AdapterBlock(nn.Module):
    def __init__(self, in_channels, out_channels, num_res_blocks, down=False):
        super().__init__()

        self.downsample = None
        if down:
            self.downsample = Downsample2D(in_channels)

        self.in_conv = None
        if in_channels != out_channels:
            self.in_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

        self.resnets = nn.Sequential(
            *[AdapterResnetBlock(out_channels) for _ in range(num_res_blocks)],
        )

    def forward(self, x):
        if self.downsample is not None:
            x = self.downsample(x)

        if self.in_conv is not None:
            x = self.in_conv(x)

        x = self.resnets(x)

        return x


class AdapterResnetBlock(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.block1 = nn.Conv2d(channels, channels, kernel_size=3, padding=1)
        self.act = nn.ReLU()
        self.block2 = nn.Conv2d(channels, channels, kernel_size=1)

    def forward(self, x):
        h = x
        h = self.block1(h)
        h = self.act(h)
        h = self.block2(h)

        return h + x


# light adapter


class LightAdapter(nn.Module):
    def __init__(
        self,
        in_channels: int = 3,
        channels: List[int] = [320, 640, 1280],
        num_res_blocks: int = 4,
        downscale_factor: int = 8,
    ):
        super().__init__()

        in_channels = in_channels * downscale_factor**2

        self.unshuffle = nn.PixelUnshuffle(downscale_factor)

        self.body = nn.ModuleList(
            [
                LightAdapterBlock(in_channels, channels[0], num_res_blocks),
                *[
                    LightAdapterBlock(channels[i], channels[i + 1], num_res_blocks, down=True)
                    for i in range(len(channels) - 1)
                ],
                LightAdapterBlock(channels[-1], channels[-1], num_res_blocks, down=True),
            ]
        )

        self.total_downscale_factor = downscale_factor * (2 ** len(channels))

    def forward(self, x):
        x = self.unshuffle(x)

        features = []

        for block in self.body:
            x = block(x)
            features.append(x)

        return features


class LightAdapterBlock(nn.Module):
    def __init__(self, in_channels, out_channels, num_res_blocks, down=False):
        super().__init__()
        mid_channels = out_channels // 4

        self.downsample = None
        if down:
            self.downsample = Downsample2D(in_channels)

        self.in_conv = nn.Conv2d(in_channels, mid_channels, kernel_size=1)
        self.resnets = nn.Sequential(*[LightAdapterResnetBlock(mid_channels) for _ in range(num_res_blocks)])
        self.out_conv = nn.Conv2d(mid_channels, out_channels, kernel_size=1)

    def forward(self, x):
        if self.downsample is not None:
            x = self.downsample(x)

        x = self.in_conv(x)
        x = self.resnets(x)
        x = self.out_conv(x)

        return x


class LightAdapterResnetBlock(nn.Module):
    def __init__(self, channels):
        super().__init__()
        self.block1 = nn.Conv2d(channels, channels, kernel_size=3, padding=1)
        self.act = nn.ReLU()
        self.block2 = nn.Conv2d(channels, channels, kernel_size=3, padding=1)

    def forward(self, x):
        h = x
        h = self.block1(h)
        h = self.act(h)
        h = self.block2(h)

        return h + x
