# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

from abc import ABC, abstractmethod
from typing import List, Optional

import torch

from executorch.examples.models.llama.llama_transformer import ModelArgs
from executorch.extension.llm.tokenizer.utils import get_tokenizer


def sample_top_p(probs, p):
    """
    Perform top-p (nucleus) sampling on a probability distribution.

    Args:
        probs (torch.Tensor): Probability distribution tensor.
        p (float): Probability threshold for top-p sampling.

    Returns:
        torch.Tensor: Sampled token indices.

    Note:
        Top-p sampling selects the smallest set of tokens whose cumulative probability mass
        exceeds the threshold p. The distribution is re-normalized based on the selected tokens.
    """
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)
    probs_sum = torch.cumsum(probs_sort, dim=-1)
    mask = probs_sum - probs_sort > p
    probs_sort[mask] = 0.0
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
    next_token = torch.multinomial(probs_sort, num_samples=1)
    next_token = torch.gather(probs_idx, -1, next_token)
    return next_token


def next_token(logits: torch.Tensor, temperature: float, top_p: float) -> int:
    if temperature > 0:
        probs = torch.softmax(logits / temperature, dim=-1)
        return sample_top_p(probs, top_p).item()
    # Pyre-ignore[7]: Incompatible return type [7]: Expected `int` but got `Union[bool, float, int]`
    return torch.argmax(logits, dim=-1).item()


class LlamaRunner(ABC):
    def __init__(self, tokenizer_path: str, model_args: ModelArgs, device: str = "cpu"):
        self.params = model_args
        self.tokenizer = get_tokenizer(tokenizer_path)
        assert model_args.vocab_size == self.tokenizer.n_words
        self.device = device

    @abstractmethod
    def forward(
        self,
        tokens: torch.Tensor,
        input_pos: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        pass

    def generate(  # noqa: C901
        self,
        prompt_tokens: List[int],
        max_seq_len: int,
        temperature: float = 0.8,
        top_p: float = 0.9,
        echo: bool = False,
        pos_base: int = 0,
    ) -> List[int]:
        # prefill
        logits = self.forward(
            tokens=torch.tensor([prompt_tokens], dtype=torch.long, device=self.device),
            input_pos=(
                torch.tensor([pos_base], dtype=torch.long, device=self.device)
                if self.params.use_kv_cache
                else None
            ),
        )

        current_token = next_token(logits, temperature, top_p)
        print(f"{self.tokenizer.decode_token(current_token)}", end="", flush=True)
        tokens = prompt_tokens + [current_token]

        while len(tokens) < max_seq_len:
            if self.params.use_kv_cache:
                logits = self.forward(
                    tokens=torch.tensor(
                        [[current_token]], dtype=torch.long, device=self.device
                    ),
                    input_pos=torch.tensor(
                        [pos_base + len(tokens) - 1],
                        dtype=torch.long,
                        device=self.device,
                    ),
                )
            else:
                logits = self.forward(
                    tokens=torch.tensor([tokens], dtype=torch.long, device=self.device),
                )
            current_token = next_token(logits, temperature, top_p)
            tokens.append(current_token)
            if current_token == self.tokenizer.eos_id or (
                hasattr(self.tokenizer, "stop_tokens")
                and current_token in self.tokenizer.stop_tokens
            ):
                break
            print(f"{self.tokenizer.decode_token(current_token)}", end="", flush=True)
        print("\n")

        return tokens if echo else tokens[len(prompt_tokens) :]

    def text_completion(
        self,
        prompt: str,
        temperature: float = 0.6,
        top_p: float = 0.9,
        echo: bool = False,
    ) -> List[int]:
        """
        Perform text completion for a prompt using the language model.

        Args:
            prompt (str): Text prompt for completion.
            temperature (float, optional): Temperature value for controlling randomness in sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling. Defaults to 0.9.
            echo (bool, optional): Flag indicating whether to include prompt tokens in the generated output. Defaults to False.

        Returns:
            Generated list of tokens.

        Note:
            This method generates text completion for the provided prompt, employing nucleus sampling to introduce controlled randomness.
        """
        return self.generate(
            prompt_tokens=self.tokenizer.encode(prompt, bos=True, eos=False),
            max_seq_len=self.params.max_seq_len,
            temperature=temperature,
            top_p=top_p,
            echo=echo,
        )

    def chat_completion(
        self,
        temperature: float = 0.6,
        top_p: float = 0.9,
    ) -> List[int]:
        """
        Perform multi-turn chat with the language model.

            Args:
                prompt (str): Text prompt for completion.
                temperature (float, optional): Temperature value for controlling randomness in sampling. Defaults to 0.6.
                top_p (float, optional): Top-p probability threshold for nucleus sampling. Defaults to 0.9.
                echo (bool, optional): Flag indicating whether to include prompt tokens in the generated output. Defaults to False.

            Returns:
                Generated list of tokens.

            Note:
                This method generates text completion for the provided prompt, employing nucleus sampling to introduce controlled randomness.
        """
        exit_prompt = "exit"
        tokens = []
        prompt = input("Me: ")
        while prompt and prompt != exit_prompt:
            print("LLM: ", end="", flush=True)
            new_tokens = self.generate(
                prompt_tokens=self.tokenizer.encode(
                    self._format_prompt(prompt), bos=True, eos=False
                ),
                max_seq_len=self.params.max_seq_len,
                temperature=temperature,
                top_p=top_p,
                echo=True,
                pos_base=len(tokens),
            )
            tokens.extend(new_tokens)
            prompt = input("Me: ")
        return tokens

    def _format_prompt(self, prompt: str) -> str:
        return f"""
<|begin_of_text|><|start_header_id|>system<|end_header_id|>

You are a helpful assistant<|eot_id|><|start_header_id|>user<|end_header_id|>

{prompt}<|eot_id|><|start_header_id|>assistant<|end_header_id|>"""
