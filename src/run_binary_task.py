import sys, os
sys.path.append(os.path.join('..', '..'))

from sklearn.metrics import classification_report
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
import random
from tqdm import tqdm, trange
from pyvene import count_parameters, set_seed
import argparse
from causal_models import DeMorgansLawCausalModels
from utils import de_morgan_sampler, generate_all_combinations_de_morgan, construct_de_morgan_input, save_results

from transformers import (GPT2Tokenizer,
                          GPT2Config,
                          AutoTokenizer,
                          GPT2ForSequenceClassification)

from pyvene import (
    IntervenableModel,
    IntervenableConfig,
    LowRankRotatedSpaceIntervention
)

# from my_pyvene.models.intervenable_base import IntervenableModel
# from my_pyvene.models.configuration_intervenable_model import IntervenableConfig, RepresentationConfig
# from my_pyvene.models.interventions import LowRankRotatedSpaceIntervention

def batched_random_sampler(data, batch_size):
    batch_indices = [_ for _ in range(int(len(data) / batch_size))]
    random.shuffle(batch_indices)
    for b_i in batch_indices:
        for i in range(b_i * batch_size, (b_i + 1) * batch_size):
            yield i

def compute_metrics(eval_preds, eval_labels):
    total_count = 0
    correct_count = 0
    for eval_pred, eval_label in zip(eval_preds, eval_labels):
        total_count += 1
        correct_count += eval_pred == eval_label
    accuracy = float(correct_count) / float(total_count)
    return {"accuracy": accuracy}
    
def calculate_loss(logits, labels):
    shift_logits = logits[..., :, :].contiguous()
    shift_labels = labels[..., :].contiguous()
    # Flatten the tokens
    loss_fct = torch.nn.CrossEntropyLoss()
    shift_logits = shift_logits.view(-1, 2) # 2 is the number of classes
    shift_labels = shift_labels.view(-1)
    # Enable model parallelism
    shift_labels = shift_labels.to(shift_logits.device).long()
    loss = loss_fct(shift_logits, shift_labels)

    return loss

def tokenizePrompt(prompt, tokenizer):
            prompt = f"{prompt['Op1']}({prompt['Op2']}({prompt['X']}) {prompt['B']} {prompt['Op3']}({prompt['Y']}))="
            return tokenizer.encode(prompt, return_tensors='pt')

def intervention_id(intervention):
    if "P" in intervention:
        return 0

def main():

    parser = argparse.ArgumentParser(description="Process experiment parameters.")
    parser.add_argument('--model_path', default='mara589/tasked-binary-gpt2', type=str, help='path to the finetuned GPT2ForSequenceClassification on the binary task')
    parser.add_argument('--results_path', type=str, default='results/binary/tasked', help='path to the results folder')
    parser.add_argument('--train_id', type=int, default=1, help='id of the model to train')
    parser.add_argument('--n_training', type=int, default=4096, help='number of training samples')
    parser.add_argument('--batch_size', type=int, default=64, help='batch size')
    parser.add_argument('--epochs', type=int, default=5, help='number of epochs for training')
    parser.add_argument('--layer', type=int, default=0, help='layer in llm where to search for an alignment')
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1, help='number of steps to accumulate before optimization step')
    parser.add_argument('--seed', type=int, default=43, help='experiment seed to be able to reproduce the results')
    args = parser.parse_args()

    size_intervention = 15

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    # device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')

    os.makedirs(args.results_path, exist_ok=True)

    save_dir_path = os.path.join(args.results_path, 'plots')
    os.makedirs(save_dir_path, exist_ok=True)
    
    set_seed(args.seed)
    total_step = 0

    # Sequence Classification with GPT2 for Binary task, n_labels=2
    tokenizer = AutoTokenizer.from_pretrained(args.model_path)
    model_config = GPT2Config.from_pretrained(args.model_path)
    model = GPT2ForSequenceClassification.from_pretrained(args.model_path, config=model_config)

    causal_model_family = DeMorgansLawCausalModels()
    train_id = args.train_id
    label = causal_model_family.get_label_by_id(train_id)
    causal_model = causal_model_family.get_model_by_id(train_id)
    # causal_model.print_structure(fig_name=f'{label}')

    print(f'Aligning with model {label}')

    all_comb = generate_all_combinations_de_morgan()

    tokenized_cache = {}
    for comb in all_comb:
        tokenized_cache[comb] = tokenizePrompt(construct_de_morgan_input(comb), tokenizer)

    print('generating data for DAS...')

    training_counterfactual_data = causal_model.generate_counterfactual_dataset(
        args.n_training,
        intervention_id,
        args.batch_size,
        device=device,
        sampler=de_morgan_sampler,
        inputFunction=lambda x: tokenized_cache[tuple(x.values())]
    )

    # for low_rank_dimension in [64, 128, 256]:
    for low_rank_dimension in [256]:
        # for layer in range(model_config.n_layer):
        layer = args.layer

        intervenable_config = IntervenableConfig({
                "layer": layer,
                "component": "block_output",
                "low_rank_dimension": low_rank_dimension,
                "unit":"pos",
                "max_number_of_units": size_intervention
            },
            intervention_types=LowRankRotatedSpaceIntervention,
            model_type=type(model)
        )

        intervenable = IntervenableModel(intervenable_config, model, use_fast=True)
        intervenable.set_device(device)
        intervenable.disable_model_gradients()

        optimizer_params = []
        for k, v in intervenable.interventions.items():
            optimizer_params += [{"params": v[0].rotate_layer.parameters()}]

        optimizer = torch.optim.Adam(optimizer_params, lr=0.01)

        print('DAS training...')

        intervenable.model.train()
        print("intervention trainable parameters: ", intervenable.count_parameters())
        print("gpt2 trainable parameters: ", count_parameters(intervenable.model))
        train_iterator = trange(0, int(args.epochs), desc="Epoch")

        for epoch in train_iterator:
            torch.cuda.empty_cache()
            epoch_iterator = tqdm(
                DataLoader(
                    training_counterfactual_data,
                    batch_size=args.batch_size,
                    sampler=batched_random_sampler(training_counterfactual_data, args.batch_size),
                ),
                desc=f"Epoch: {epoch}",
                position=0,
                leave=True,
            )
            
            for step, inputs in enumerate(epoch_iterator):
                for k, v in inputs.items():
                    if v is not None and isinstance(v, torch.Tensor):
                        inputs[k] = v.to(device)
                inputs["input_ids"] = inputs["input_ids"].squeeze().long()
                inputs["source_input_ids"] = inputs["source_input_ids"].squeeze(2).long()
                # b_s = inputs["input_ids"].shape[0]

                _, counterfactual_outputs = intervenable(
                    {"input_ids": inputs["input_ids"]},
                    [{"input_ids": inputs["source_input_ids"][:, 0]}],
                    {
                        "sources->base": list(range(size_intervention))
                    },
                    subspaces=[
                        [[_ for _ in range(low_rank_dimension)]] * args.batch_size
                    ]
                )

                eval_metrics = compute_metrics(
                    counterfactual_outputs[0].argmax(1), inputs["labels"].squeeze()
                )

                # loss and backprop
                loss = calculate_loss(counterfactual_outputs.logits, inputs["labels"])
                loss_str = round(loss.item(), 2)
                epoch_iterator.set_postfix({"loss": loss_str, "acc": eval_metrics["accuracy"]})

                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps
                loss.backward(retain_graph=True) 

                if total_step % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    intervenable.set_zero_grad()
                total_step += 1

        intervenable_path = os.path.join(args.results_path, f'intervenable_models/{label}/intervenable_{low_rank_dimension}_{layer}')
        intervenable.save(intervenable_path)

if __name__ =="__main__":
    main()