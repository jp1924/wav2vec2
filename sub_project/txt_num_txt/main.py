import json
import os
from pathlib import Path

from datasets import Dataset, load_dataset
from evaluate import load
from peft import PeftConfig
from peft.utils import get_peft_model_state_dict
from setproctitle import setproctitle
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    HfArgumentParser,
    LlamaConfig,
    LlamaForCausalLM,
    Seq2SeqTrainer,
    deepspeed,
    is_torch_xla_available,
    is_wandb_available,
)
from transformers import logging as hf_logging
from transformers import set_seed
from trl import SFTTrainer
from utils import TNTTrainingArguments

hf_logging.set_verbosity_info()
logger = hf_logging.get_logger("transformers")

GLOBAL_LOGGER = None


def main(train_args: TNTTrainingArguments) -> None:
    def preprocess():
        return

    def set_wandb() -> None:
        # TODO: 이건 나중에 args로 바꿀 것
        GLOBAL_LOGGER.run.log_code(
            "/root/workspace",
            include_fn=lambda path: path.endswith(".py") or path.endswith(".json"),
        )
        # logging args
        combined_dict = {**train_args.to_dict()}
        if hasattr(model, "config") and model.config is not None:
            model_config = model.config.to_dict()
            combined_dict = {**model_config, **combined_dict}

        GLOBAL_LOGGER.config.update(combined_dict, allow_val_change=True)

        # set default metrics
        if getattr(GLOBAL_LOGGER, "define_metric", None):
            GLOBAL_LOGGER.define_metric("train/global_step")
            GLOBAL_LOGGER.define_metric("*", step_metric="train/global_step", step_sync=True)

        # set model watch
        _watch_model = os.getenv("WANDB_WATCH", "false")
        if not is_torch_xla_available() and _watch_model in ("all", "parameters", "gradients"):
            GLOBAL_LOGGER.watch(model, log=_watch_model, log_freq=max(100, train_args.logging_steps))
        GLOBAL_LOGGER.run._label(code="transformers_trainer")

    def get_model_init_kwargs() -> dict:
        model_init_kwargs = {}
        if train_args.quantization_config_path:
            quantization_config_path_txt = Path(train_args.quantization_config_path).read_text()
            quantization_config = json.loads(quantization_config_path_txt)

            quantization_config, unused_config = BitsAndBytesConfig.from_dict(
                quantization_config,
                return_unused_kwargs=True,
            )

            logger.info("quantization_config")
            logger.info(quantization_config)
            logger.info("unused_config")
            logger.info(unused_config)

            model_init_kwargs["quantization_config"] = quantization_config

        model_init_kwargs["use_cache"] = False
        model_init_kwargs["device_map"] = train_args.device_map
        model_init_kwargs["torch_dtype"] = train_args.torch_dtype
        model_init_kwargs["use_auth_token"] = train_args.use_auth_token
        model_init_kwargs["low_cpu_mem_usage"] = train_args.low_cpu_mem_usage
        model_init_kwargs["trust_remote_code"] = train_args.trust_remote_code
        model_init_kwargs["use_flash_attention_2"] = train_args.use_flash_attention_2

        return model_init_kwargs

    model_init_kwargs = get_model_init_kwargs()
    config = AutoConfig.from_pretrained(train_args.model_name_or_path, use_cache=False)
    model = AutoModelForCausalLM.from_pretrained(train_args.model_name_or_path, config=config, **model_init_kwargs)
    tokenizer = AutoTokenizer.from_pretrained(train_args.model_name_or_path)

    if GLOBAL_LOGGER and (train_args.local_rank == 0):
        set_wandb()

    peft_config = None
    if train_args.do_peft:
        peft_config = PeftConfig.from_pretrained(train_args.peft_config_name_or_path)

    train_data = None
    valid_data = None

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        peft_config=peft_config,
        train_dataset=train_data,
        eval_dataset=valid_data,
        args=train_args,
    )

    # [NOTE]: run train, eval, predict
    if train_args.do_train:
        train(trainer, train_args)
    if train_args.do_eval:
        eval(trainer, valid_data)
    if train_args.do_predict:
        predict(trainer, valid_data)


def train(trainer: SFTTrainer) -> None:
    # copied from peft.utils.get_peft_model_state_dict
    def get_peft_state_maybe_zero_3(named_params, bias):
        from deepspeed import zero
        from deepspeed.runtime.zero.partition_parameters import ZeroParamStatus

        def maybe_zero_3(param):
            if hasattr(param, "ds_id"):
                assert param.ds_status == ZeroParamStatus.NOT_AVAILABLE
                with zero.GatheredParameters([param]):
                    param = param.data.detach().cpu().clone()
            else:
                param = param.detach().cpu().clone()
            return param

        if bias == "none":
            to_return = {k: t for k, t in named_params if "lora_" in k}
        elif bias == "all":
            to_return = {k: t for k, t in named_params if "lora_" in k or "bias" in k}
        elif bias == "lora_only":
            to_return = {}
            maybe_lora_bias = {}
            lora_bias_names = set()
            for k, t in named_params:
                if "lora_" in k:
                    to_return[k] = t
                    bias_name = k.split("lora_")[0] + "bias"
                    lora_bias_names.add(bias_name)
                elif "bias" in k:
                    maybe_lora_bias[k] = t
            for k, t in maybe_lora_bias:
                if bias_name in lora_bias_names:
                    to_return[bias_name] = t
        else:
            raise NotImplementedError
        to_return = {k: maybe_zero_3(v) for k, v in to_return.items()}
        return to_return

    train_args = trainer.args
    outputs = trainer.train(resume_from_checkpoint=train_args.resume_from_checkpoint)
    metrics = outputs.metrics

    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    # NOTE: peft + zero3가 적용된 상태에서 어떻게 저장되는지 확인
    trainer.save_model(train_args.output_dir)

    if deepspeed.is_deepspeed_zero3_enabled():
        # use deepspeed engine internal function to gather state dict
        # state_dict_zero3 contains whole parameters of base and lora adapters
        # we will not extract lora parameters since peft save_pretrained will do that
        # https://github.com/huggingface/peft/blob/3714aa2fff158fdfa637b2b65952580801d890b2/src/peft/peft_model.py#L125
        # https://github.com/huggingface/peft/blob/3714aa2fff158fdfa637b2b65952580801d890b2/src/peft/utils/save_and_load.py#L19
        state_dict_zero3 = trainer.model_wrapped._zero3_consolidated_16bit_state_dict()
        if train_args.local_rank == 0:
            state_dict = state_dict_zero3
    else:
        # in other mode we use original code from fastchat team, to make sure our change is minimum
        state_dict = get_peft_state_maybe_zero_3(trainer.model.named_parameters(), lora_args.bias)
    if train_args.local_rank == 0:
        trainer.model.save_pretrained(train_args.output_dir, state_dict=state_dict)
        trainer.tokenizer.save_pretrained(train_args.output_dir)


def eval(trainer: SFTTrainer) -> None:
    trainer.evaluate()


def predict(trainer: Seq2SeqTrainer, test_data: Dataset) -> None:
    trainer.args.predict_with_generate = True
    trainer.predict(test_data)


if __name__ == "__main__":
    parser = HfArgumentParser([TNTTrainingArguments])
    train_args, _ = parser.parse_args_into_dataclasses(return_remaining_strings=True)

    if train_args.seed is not None:
        set_seed(train_args.seed)

    if train_args.run_name is not None:
        setproctitle(train_args.run_name)

    check_wandb = ("wandb" in train_args.report_to) and (train_args.local_rank == 0)
    if is_wandb_available() and check_wandb:
        import wandb

        wandb.init(
            project=os.getenv("WANDB_PROJECT"),
            entity=os.getenv("WANDB_ENTITY"),
            group=os.getenv("WANDB_RUN_GROUP"),
            name=train_args.run_name,
            save_code=True,
        )
        GLOBAL_LOGGER = wandb

    main(train_args)