import sys, os
sys.path.append(os.path.join('..', '..'))

from sklearn.metrics import classification_report
from tqdm import tqdm
import torch
from torch.utils.data import DataLoader
import random
from pyvene import set_seed
import argparse
from causal_models import DeMorgansLawCausalModels
from utils import de_morgan_sampler, generate_all_combinations_de_morgan, construct_de_morgan_input, save_binary_results

from transformers import (GPT2Tokenizer,
                          GPT2Config,
                          AutoTokenizer,
                          GPT2ForSequenceClassification)

from pyvene import IntervenableModel

def tokenizePrompt(prompt, tokenizer):
    prompt = f"{prompt['Op1']}({prompt['Op2']}({prompt['X']}) {prompt['B']} {prompt['Op3']}({prompt['Y']}))="
    return tokenizer.encode(prompt, return_tensors='pt')

def intervention_id(intervention):
    if "P" in intervention:
        return 0

def compute_metrics(eval_preds, eval_labels):
    total_count = 0
    correct_count = 0
    for eval_pred, eval_label in zip(eval_preds, eval_labels):
        total_count += 1
        correct_count += eval_pred == eval_label
    accuracy = float(correct_count) / float(total_count)
    return {"accuracy": accuracy}

def batched_random_sampler(data, batch_size):
    batch_indices = [_ for _ in range(int(len(data) / batch_size))]
    random.shuffle(batch_indices)
    for b_i in batch_indices:
        for i in range(b_i * batch_size, (b_i + 1) * batch_size):
            yield i

def eval_intervenable(intervenable, eval_data, batch_size, low_rank_dimension, size_intervention, device):
    # eval on all data
    eval_labels = []
    eval_preds = []
    with torch.no_grad():
        epoch_iterator = tqdm(DataLoader(eval_data, batch_size), desc=f"Test")

        for step, inputs in enumerate(epoch_iterator):
            for k, v in inputs.items():
                if v is not None and isinstance(v, torch.Tensor):
                    inputs[k] = v.to(device)

            inputs["input_ids"] = inputs["input_ids"].squeeze().long()
            inputs["source_input_ids"] = inputs["source_input_ids"].squeeze(2).long()

            _, counterfactual_outputs = intervenable(
                {"input_ids": inputs["input_ids"]},
                [{"input_ids": inputs["source_input_ids"][:,0]}],
                {
                    "sources->base": list(range(size_intervention))
                },
                subspaces=[
                    [[_ for _ in range(low_rank_dimension)]] * batch_size
                ]
            )

            eval_labels += [inputs["labels"].type(torch.long).squeeze()]
            eval_preds += [torch.argmax(counterfactual_outputs[0], dim=1)]

            eval_metrics = compute_metrics(
                counterfactual_outputs[0].argmax(1), inputs["labels"].squeeze()
            )

            epoch_iterator.set_postfix({"acc": eval_metrics["accuracy"]})

    report = classification_report(torch.cat(eval_labels).cpu(), torch.cat(eval_preds).cpu(), output_dict=True) # get the IIA
    return report

def main():

    parser = argparse.ArgumentParser(description="Process experiment parameters.")
    parser.add_argument('--model_path', type=str, default='mara589/tasked-binary-gpt2', help='path to the finetuned GPT2ForSequenceClassification on the binary task')
    parser.add_argument('--results_path', type=str, default='results/binary/tasked', help='path to the results folder')
    parser.add_argument('--train_id', type=int, default=1, help='id of the model to train')
    parser.add_argument('--n_testing', type=int, default=256, help='number of testing samples')
    parser.add_argument('--layer', type=int, default=0, help='layer in llm where to search for an alignment')
    parser.add_argument('--batch_size', type=int, default=128, help='batch size')
    parser.add_argument('--intervenable_model_path', type=str, help='batch size')
    parser.add_argument('--seed', type=int, default=43, help='experiment seed to be able to reproduce the results')
    args = parser.parse_args()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    size_intervention = 15
    # intervenable_model_path = 'mara589/binary-tasked-intervenable-models'

    os.makedirs(args.results_path, exist_ok=True)

    save_dir_path = os.path.join(args.results_path, 'plots')
    os.makedirs(save_dir_path, exist_ok=True)
    
    set_seed(args.seed)

    tokenizer = AutoTokenizer.from_pretrained(args.model_path)
    model_config = GPT2Config.from_pretrained(args.model_path)
    model = GPT2ForSequenceClassification.from_pretrained(args.model_path, config=model_config)

    causal_model_family = DeMorgansLawCausalModels()

    all_comb = generate_all_combinations_de_morgan()

    tokenized_cache = {}
    for comb in all_comb:
        tokenized_cache[comb] = tokenizePrompt(construct_de_morgan_input(comb), tokenizer)

    label = causal_model_family.get_label_by_id(args.train_id)
    causal_model = causal_model_family.get_model_by_id(args.train_id)

    print(f'Aligning with model {label}')

    testing_counterfactual_data = causal_model.generate_counterfactual_dataset(
        args.n_testing,
        intervention_id,
        args.batch_size,
        device=device,
        sampler=de_morgan_sampler,
        inputFunction=lambda x: tokenized_cache[tuple(x.values())]
    )
    
    low_rank_dimension = 256
    layer = args.layer
    # for layer in range(12):
    # subfolder = f'{label}/intervenable_{low_rank_dimension}_{layer}'
    # intervenable = IntervenableModel.load(intervenable_model_path, model=model, subfolder=subfolder)

    # intervenable_model_path = os.path.join(args.results_path, f'intervenable_models/{label}/intervenable_{low_rank_dimension}_{layer}')
    intervenable = IntervenableModel.load(args.intervenable_model_path, model=model)
    
    intervenable.set_device(device)
    intervenable.disable_model_gradients()

    report = eval_intervenable(intervenable, testing_counterfactual_data, args.batch_size, low_rank_dimension, size_intervention, device)
    save_binary_results(args.results_path, report, layer, low_rank_dimension, label)
        
if __name__ =="__main__":
    main()