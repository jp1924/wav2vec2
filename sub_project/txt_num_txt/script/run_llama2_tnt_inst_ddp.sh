export WANDB_PROJECT=""
export WANDB_RUN_GROUP=""
export WANDB_DISABLE_CODE=""
export WANDB_ENTITY=""
export WANDB_WATCH=""
export WANDB_API_KEY=""
export WANDB_DISABLED=""

export TORCH_DISTRIBUTED_DEBUG="DETAIL"
export TORCHDYNAMO_DISABLE="1"


torchrun --nproc_per_node=8 \
    /root/workspace//sub_project/txt_num_txt/main.py \
    --output_dir=/root/output_dir \
    --run_name=TNT-llama2 \
    --model_name_or_path=beomi/llama-2-ko-7b \
    --preprocessing_num_workers=10 \
    --per_device_train_batch_size=24 \
    --gradient_accumulation_steps=1 \
    --per_device_eval_batch_size=2 \
    --num_train_epochs=1 \
    --seed=42 \
    --do_train=true \
    --do_eval=false \
    --do_predict=true \
    --report_to=none \
    --learning_rate=3e-5 \
    --warmup_ratio=0.4 \
    --weight_decay=0.01 \
    --evaluation_strategy=no \
    --save_strategy=no \
    --logging_strategy=steps \
    --lr_scheduler_type=cosine \
    --logging_steps=1 \
    --bf16=true \
    --dataset_names=jp1924/TNT_inst \
    --gradient_checkpointing=true \
    --torch_compile=true \
    --group_by_length=false \
    --fsdp="full_shard auto_wrap" \
    --fsdp_transformer_layer_cls_to_wrap=LlamaDecoderLayer
