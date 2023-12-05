import itertools
import logging

import numpy as np
import torch
import torch.nn as nn
from torch.functional import F
from tqdm.auto import tqdm

from skimage.measure import label
from scipy.ndimage import distance_transform_edt

import denoiseg.training as tr

logger = logging.getLogger("denoiseg")


class StopTrainingException(Exception):
    pass


class EmptyCallable:
    def __call__(self):
        return None


class EarlyStopper:
    def __init__(self, patience=1, min_delta=0):
        self.patience = patience
        self.min_delta = min_delta
        self.counter = 0
        self.min_validation_loss = np.inf

    def __call__(self, validation_loss):
        return self.early_stop(validation_loss)

    def early_stop(self, validation_loss):
        if validation_loss < self.min_validation_loss:
            self.min_validation_loss = validation_loss
            self.counter = 0
        elif validation_loss > (self.min_validation_loss + self.min_delta):
            self.counter += 1
            logger.debug(f"Early stopper's patience {self.patience=} update. ({self.counter})")
            if self.counter >= self.patience:
                logger.info(
                    f"Early stopper's {self.patience=} run out with {self.min_validation_loss=:.5}"
                )
                raise StopTrainingException()


class MetricCheckpointer:
    def __init__(self, model, model_path, min_delta=0):
        self.model = model
        self.model_path = model_path
        self.min_validation_loss = np.inf
        self.min_delta = min_delta

    def __call__(self, validation_loss):
        return self.checkpoint_if_best(validation_loss)

    def checkpoint_if_best(self, validation_loss):
        if self.model_path is None:
            return False

        if validation_loss > self.min_validation_loss + self.min_delta:
            return False

        before_val_loss = self.min_validation_loss
        self.min_validation_loss = validation_loss

        best_val_loss = self.min_validation_loss
        logger.info(
            f"checkpoint best model {before_val_loss=:.5} -> {best_val_loss:.5}"
        )

        torch.save(self.model, self.model_path)
        return True


# https://www.kaggle.com/code/bigironsphere/loss-function-library-keras-pytorch#Dice-Loss
class FocalLoss(nn.Module):
    def __init__(self, alpha=0.8, gamma=2, reduction="mean"):
        super(FocalLoss, self).__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.reduction = reduction

    def forward(self, inputs, targets):
        # comment out if your model contains a sigmoid or equivalent activation layer
        # inputs = torch.sigmoid(inputs)

        bce = F.binary_cross_entropy(inputs, targets, reduction=self.reduction)
        bce_exp = torch.exp(-bce)
        return self.alpha * (1 - bce_exp) ** self.gamma * bce


class DiceLoss(nn.Module):
    def __init__(self, smooth=1, reduction="none"):
        super(DiceLoss, self).__init__()
        self.smooth = smooth
        self.reduction = reduction

    def forward(self, inputs, targets):
        # comment out if your model contains a sigmoid or equivalent activation layer
        # inputs = F.sigmoid(inputs)

        # flatten label and prediction tensors
        # inputs = inputs.view(-1)
        # targets = targets.view(-1)

        # intersection = (inputs * targets).sum()
        intersection = inputs * targets
        dice = (2.0 * intersection + self.smooth) / (
            inputs.sum() + targets.sum() + self.smooth
        )

        loss = 1 - dice
        if self.reduction == "none":
            return loss
        if self.reduction == "mean":
            return torch.mean(loss)
        if self.reduction == "sum":
            return torch.sum(loss)
        else:
            raise ValueError()


def step(model, targets, loss_fn, device="cpu"):
    device_targets = {k: v.to(device) for k, v in targets.items()}
    pred = model(device_targets["x"])
    return loss_fn(device_targets,pred)


def train_epoch(model, dataloader, optimizer, step_fn):
    model.train()

    def train_step(targets):
        optimizer.zero_grad()
        ls = step_fn(targets)
        ls.backward()
        optimizer.step()
        return ls.item()

    losses = [train_step(targets) for targets in dataloader]
    return np.mean(losses)


def validate_epoch(model, dataloader, step_fn):
    model.eval()
    with torch.no_grad():
        losses = [step_fn(t).item() for t in dataloader]
        return np.mean(losses)


def run_epoch(
    train_epoch_fn,
    validate_epoch_fn,
    after_callbacks,
):
    train_loss = train_epoch_fn()
    val_loss = validate_epoch_fn()

    # TODO?
    # evaluator.evaluate_on_epoch(model,epoch)

    for callback in after_callbacks:
        callback(val_loss)
    return train_loss, val_loss


def train(
    model,
    train_dataloader,
    val_dataloader,
    loss_fn,
    epochs=0,
    patience=None,
    scheduler_patience=None,
    checkpoint_path=None,
    lr=0.001,
    device="cpu",
):
    model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    def step_fn(targets):
        return step(model, targets, loss_fn, device=device)

    def train_epoch_fn():
        return train_epoch(model, train_dataloader, optimizer, step_fn)

    def eval_epoch_fn():
        return validate_epoch(model, val_dataloader, step_fn)

    after_callbacks = []
    if scheduler_patience is not None:
        after_callbacks += [tr.setup_scheduler(optimizer, scheduler_patience)]

    if patience is not None:
        after_callbacks += [tr.EarlyStopper(patience=patience, min_delta=0)]

    if checkpoint_path is not None:
        after_callbacks += [tr.MetricCheckpointer(model, checkpoint_path)]

    train_losses = []
    validation_losses = []
    epochs_iter = range(epochs) if epochs is not None else itertools.count()
    for epoch in tqdm(epochs_iter, desc="Training epochs"):
        try:
            loss_train, loss_val = run_epoch(
                train_epoch_fn, eval_epoch_fn, after_callbacks
            )
            train_losses.append(loss_train)
            validation_losses.append(loss_val)

            logger.info(f"{epoch=} {loss_val=:.5f}")
        except tr.StopTrainingException:
            break

    loss_dict = {"train_loss": train_losses, "val_loss": validation_losses}
    return loss_dict


def _get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']
    
def setup_scheduler(optimizer, scheduler_patience):
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, "min", patience=scheduler_patience
    )

    def scheduler_fn(validation_loss):
        scheduler.step(validation_loss)
        lr = _get_lr(optimizer)
        logger.debug(f"learning rate: {lr}")

    return scheduler_fn


def resolve_loss(loss_name):
    losses = {
        "dice": lambda: DiceLoss(reduction="none"),
        "bc": lambda: nn.BCEWithLogitsLoss(reduction="none"),
        "fl": lambda: FocalLoss(reduction="none"),
    }
    return losses[loss_name]()


def get_loss(
    loss_name="fl", 
    device="cpu", 
    denoise_loss_weight=1,
    denoise_enabled = True
):
    logger.info(f"Setting up loss. {denoise_loss_weight=}, {denoise_enabled=}")
    seg_loss = resolve_loss(loss_name).to(device)
    loss_denoise = nn.MSELoss().to(device)

    def calc_loss(targets, prediction):
        y_segmentation = targets["y_segmentation"]
        has_label = targets["has_label"]
        weightmap = targets["weightmap"]

        pred_segm = prediction[:, -3:]

        ls_seg_pure = seg_loss(pred_segm, y_segmentation)
        ls_seg_only_valid = ls_seg_pure * has_label
        loss = (ls_seg_only_valid * weightmap).mean()
    
        if denoise_enabled:
            pred_denoise = prediction[:, 0][:, None, ...]
            y_denoise = targets["y_denoise"]
            mask_denoise = targets["mask_denoise"]
            pred_denoise_masked = pred_denoise * mask_denoise

            y_denoise_masked = y_denoise * mask_denoise
            ls_denoise = loss_denoise(y_denoise_masked,pred_denoise_masked)

            
            loss+= ls_denoise * denoise_loss_weight
        
        return loss

    return calc_loss


def unet_weight_map(y, wc=None, w0 = 10, sigma = 5):
    
    labels = label(y)
    no_labels = labels == 0
    label_ids = sorted(np.unique(labels))[1:]

    if len(label_ids) > 1:
        distances = np.zeros((y.shape[0], y.shape[1], len(label_ids)))

        for i, label_id in enumerate(label_ids):
            distances[:,:,i] = distance_transform_edt(labels != label_id)

        distances = np.sort(distances, axis=2)
        d1 = distances[:,:,0]
        d2 = distances[:,:,1]
        w = w0 * np.exp(-1/2*((d1 + d2) / sigma)**2) * no_labels
        
        if wc:
            class_weights = np.zeros_like(y)
            for k, v in wc.items():
                class_weights[y == k] = v
            w = w + class_weights
        else:
            w = w +1
    else:
        w = np.zeros_like(y)
    
    return w
