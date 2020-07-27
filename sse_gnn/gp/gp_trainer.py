"""Utilities for training a GP fed from the MEGNet Concatenation layer for a pretrained model."""
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

import numpy as np
import tensorflow as tf
import tensorflow.python.util.deprecation as deprecation
import tensorflow_probability as tfp
from tqdm import tqdm

from ..datalib.visualisation import plot_calibration, plot_sharpness

deprecation._PRINT_DEPRECATION_WARNINGS = False


tfd = tfp.distributions
tfk = tfp.math.psd_kernels


def convert_index_points(array: np.ndarray) -> tf.Tensor:
    """Reshape an array into a tensor appropriate for GP index points.

    Extends the amount of dimensions by `array.shape[1] - 1` and converts
    to a `Tensor` with `dtype=tf.float64`.

    Args:
        array (:obj:`np.ndarray`): The array to extend.

    Returns
        tensor (:obj:`tf.Tensor`): The converted Tensor.

    """
    shape = array.shape
    shape += (1,) * (shape[1] - 1)
    return tf.constant(array, dtype=tf.float64, shape=shape)


class GPTrainer(tf.Module):
    """Class for training hyperparameters for GP kernels.

    Args:
        observation_index_points (:obj:`tf.Tensor`): The observed index points (_x_ values).
        observations (:obj:`tf.Tensor`): The observed samples (_y_ values).
        checkpoint_dir (str or :obj:`Path`, optional): The directory to check for
            checkpoints and to save checkpoints to.

    Attributes:
        observation_index_points (:obj:`tf.Tensor`): The observed index points (_x_ values).
        observations (:obj:`tf.Tensor`): The observed samples (_y_ values).
        checkpoint_dir (str or :obj:`Path`, optional): The directory to check for
            checkpoints and to save checkpoints to.
        amplitude (:obj:`tf.Tensor`): The amplitude of the kernel.
        length_scale (:obj:`tf.Tensor`): The length scale of the kernel.
        kernel (:obj:`tf.Tensor`): The kernel to use for the Gaussian process.
        optimizer (:obj:`Optimizer`): The optimizer to use for determining
            :attr:`amplitude` and :attr:`length_scale`.
        training_steps (:obj:tf.Tensor): The current number of training epochs executed.
        loss (:obj:`tf.Tensor`): The current loss on the training data
            (A negative log likelihood).
        metrics (dict): Contains metric names and values.
            Default to `np.nan` when uncalculated.
        ckpt (:obj:`Checkpoint`, optional): A tensorflow training checkpoint.
            Defaults to `None` if `checkpoint_dir` is not passed.
        ckpt_manager (:obj:`CheckpointManager`, optional): A checkpoint manager, used to save
            :attr:`ckpt` to file.
            Defaults to `None` if `checkpoint_dir` is not passed.
        gp_prior (:obj:`GaussianProcess`): A Gaussian process using :attr:`kernel` and
            using :attr:`observation_index_points` as indices.

    """

    def __init__(
        self,
        observation_index_points: tf.Tensor,
        observations: tf.Tensor,
        checkpoint_dir: Optional[Union[str, Path]] = None,
    ):
        """Initialze attributes, kernel, optimizer and checkpoint manager."""
        self.observation_index_points = tf.Variable(
            observation_index_points,
            dtype=tf.float64,
            trainable=False,
            name="observation_index_points",
        )
        self.observations = tf.Variable(
            observations, dtype=tf.float64, trainable=False, name="observations",
        )

        self.amplitude = tf.Variable(1.0, dtype=tf.float64, name="amplitude")
        self.length_scale = tf.Variable(1.0, dtype=tf.float64, name="length_scale")

        # TODO: Customizable kernel
        self.kernel = tfk.MaternOneHalf(
            amplitude=self.amplitude,
            length_scale=self.length_scale,
            feature_ndims=self.observation_index_points.shape[1],
        )

        self.optimizer = tf.optimizers.Adam()

        self.training_steps = tf.Variable(
            0, dtype=tf.int32, trainable=False, name="training_steps"
        )

        self.loss = tf.Variable(
            np.nan, dtype=tf.float64, trainable=False, name="training_nll",
        )

        self.metrics = {
            "nll": tf.Variable(
                np.nan, dtype=tf.float64, trainable=False, name="validation_nll",
            ),
            "mae": tf.Variable(
                np.nan, dtype=tf.float64, trainable=False, name="validation_mae",
            ),
            "sharpness": tf.Variable(
                np.nan, dtype=tf.float64, trainable=False, name="validation_sharpness",
            ),
            "variation": tf.Variable(
                np.nan,
                dtype=tf.float64,
                trainable=False,
                name="validation_coeff_variance",
            ),
            "calibration_err": tf.Variable(
                np.nan,
                dtype=tf.float64,
                trainable=False,
                name="validation_calibration_error",
            ),
        }

        if checkpoint_dir:
            self.ckpt = tf.train.Checkpoint(
                step=self.training_steps,
                amp=self.amplitude,
                ls=self.length_scale,
                loss=self.loss,
                val_nll=self.metrics["nll"],
                val_mae=self.metrics["mae"],
                val_sharpness=self.metrics["sharpness"],
                val_coeff_var=self.metrics["variation"],
                val_cal_err=self.metrics["calibration_err"],
            )
            self.ckpt_manager = tf.train.CheckpointManager(
                self.ckpt,
                checkpoint_dir,
                max_to_keep=1,
                step_counter=self.training_steps,
            )

            self.ckpt.restore(self.ckpt_manager.latest_checkpoint)
            if self.ckpt_manager.latest_checkpoint:
                print(f"Restored from {self.ckpt_manager.latest_checkpoint}")
            else:
                print("No checkpoints found.")

        else:
            self.ckpt = None
            self.ckpt_manager = None

        self.gp_prior = tfd.GaussianProcess(self.kernel, self.observation_index_points)

    @staticmethod
    def load_model(model_dir: str):
        """Load a `GPTrainer` model from a file.

        Args:
            model_dir (str): The directory to import the model from.

        Returns:
            The model as a TensorFlow AutoTrackable object.

        """
        return tf.saved_model.load(model_dir)

    def get_model(
        self, index_points: tf.Tensor
    ) -> tfp.python.distributions.GaussianProcessRegressionModel:
        """Get a regression model for a set of index points.

        Args:
            index_points (:obj:`tf.Tensor`): The index points to fit
                regression model.

        Returns:
            gprm (:obj:`GaussianProcessRegressionModel`): The regression model.

        """
        return tfd.GaussianProcessRegressionModel(
            kernel=self.kernel,
            index_points=index_points,
            observation_index_points=self.observation_index_points,
            observations=self.observations,
        )

    @tf.function(input_signature=[tf.TensorSpec(None, tf.float64)])
    def predict(self, points: tf.Tensor) -> Tuple[tf.Tensor, tf.Tensor]:
        """Predict targets and the standard deviation of the distribution.

        Args:
            points (:obj:`tf.Tensor`): The points (_x_ values) to make predictions with.

        Returns:
            mean (:obj:`tf.Tensor`): The mean of the distribution at each point.
            stddev (:obj:`tf.Tensor`): The standard deviation of the distribution
                at each point.

        """
        gprm = self.get_model(points)
        return gprm.mean(), gprm.stddev()

    def train_model(
        self,
        val_points: tf.Tensor,
        val_obs: tf.Tensor,
        epochs: int = 1000,
        patience: Optional[int] = None,
        save_dir: Optional[Union[str, Path]] = None,
        metrics: List[str] = [],
    ) -> Iterator[Dict[str, float]]:
        """Optimize model parameters.

        Args:
            val_points (:obj:`tf.Tensor`): The validation points.
            val_obs (:obj:`tf.Tensor`): The validation targets.
            epochs (int): The number of training epochs.
            patience (int, optional): The number of epochs after which to
                stop training if no improvement is seen on the loss of the
                validation data.
            save_dir (str or :obj:`Path`, optional): Where to save the model.
            metrics (list of str): A list of valid metrics to calculate.
                Possible valid metrics are given in :class:`GPMetrics`.

        Yields:
            metrics (dict of str: float): A dictionary of the metrics after the
                last training epoch.

        """
        best_val_nll: float = self.metrics["nll"].numpy()
        if np.isnan(best_val_nll):
            # Set to infinity so < logic works
            best_val_nll = np.inf

        if (self.ckpt_manager or patience) and "nll" not in metrics:
            # We need to track NLL for these to work
            metrics.append("nll")

        steps_since_improvement: int = 1
        gp_metrics = GPMetrics(val_points, val_obs, self)

        for i in tqdm(range(epochs), "Training epochs"):
            self.loss.assign(self.optimize_cycle())
            self.training_steps.assign_add(1)

            # * Determine and assign metrics
            if gp_metrics.REQUIRES_MEAN.intersection(metrics):
                gp_metrics.update_mean()
            if gp_metrics.REQUIRES_STDDEV.intersection(metrics):
                gp_metrics.update_stddevs()

            try:
                metric_dict: Dict[str, float] = {
                    metric: getattr(gp_metrics, metric) for metric in metrics
                }
            except AttributeError as e:
                raise ValueError(f"Invalid metric: {e}")

            for metric, value in metric_dict.items():
                self.metrics[metric].assign(value)

            metric_dict["loss"] = self.loss.numpy()
            yield metric_dict

            if patience or self.ckpt_manager:
                if self.metrics["nll"] < best_val_nll:
                    best_val_nll = self.metrics["nll"].numpy()
                    steps_since_improvement = 1
                    if self.ckpt_manager:
                        self.ckpt_manager.save(self.training_steps)
                else:
                    steps_since_improvement += 1
                    if patience and steps_since_improvement >= patience:
                        print(
                            "Patience exceeded: "
                            f"{steps_since_improvement} steps since NLL improvement."
                        )
                        break

        if save_dir:
            tf.saved_model.save(self, save_dir)

    @tf.function
    def optimize_cycle(self) -> tf.Tensor:
        """Perform one training step.

        Returns:
            loss (:obj:`Tensor`): A Tensor containing the negative log probability loss.

        """
        with tf.GradientTape() as tape:
            loss = -self.gp_prior.log_prob(self.observations)

        grads = tape.gradient(loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(grads, self.trainable_variables))
        return loss


class GPMetrics:
    """Handler for GP metric calculations.

    Many of the metrics herein are based upon implementations by `Train et al.`_,
    for metrics proposed by `Kuleshov et al.`_.

    Args:
        val_points (:obj:`tf.Tensor`): The validation indices.
        val_obs (:obj:`np.ndarray`): The validation observed true values.
        gp_trainer (:obj:`GPTrainer`): The :obj:`GPTrainer` instance to
            analyse.

    Attributes:
        val_points (:obj:`tf.Tensor`): The validation indices.
        val_obs (:obj:`np.ndarray`): The validation observed true values.
        gp_trainer (:obj:`GPTrainer`): The :obj:`GPTrainer` instance to
            analyse.
        gprm (:obj:`GaussianProcessRegressionModel`): A regression model from
            `gp_trainer` fit to the `val_points`.

    .. _Tran et al.:
        https://arxiv.org/abs/1912.10066
    .. _Kuleshov et al.:
        https://arxiv.org/abs/1807.00263

    """

    # Set of which properties need the mean and standard deviation to be updated
    REQUIRES_MEAN = {"mae", "calibration_err", "residuals", "pis"}
    REQUIRES_STDDEV = {"sharpness", "variation"}

    def __init__(
        self, val_points: tf.Tensor, val_obs: tf.Tensor, gp_trainer: GPTrainer
    ):
        """Initialize validation points and observations and the wrapped `GPTrainer`."""
        self.val_points = val_points
        self.val_obs = val_obs
        self.gp_trainer = gp_trainer
        self.gprm = gp_trainer.get_model(val_points)  # Instantiate GPRM
        self.update_mean()
        self.update_stddevs()

    def update_mean(self):
        """Update the GPRM mean predictions."""
        self.mean = self.gprm.mean().numpy()

    def update_stddevs(self):
        """Update the GPRM standard deviation predictions."""
        self.stddevs = self.gprm.stddev().numpy()

    @property
    def nll(self) -> float:
        """Calculate the negative log likelihood of observed true values.

        Returns:
            nll (float)

        """
        return -self.gprm.log_prob(self.val_obs).numpy()

    @property
    def mae(self) -> float:
        """Calculate the mean average error of predicted values.

        Returns:
            mean (float)

        """
        return tf.losses.mae(self.val_obs, self.mean).numpy()

    @property
    def sharpness(self) -> float:
        """Calculate the root-mean-squared of predicted standard deviations.

        Returns:
            sharpness (float)

        """
        return np.sqrt(np.mean(np.square(self.stddevs)))

    @property
    def variation(self) -> float:
        """Calculate the coefficient of variation of the regression model.

        Indicates dispersion of uncertainty estimates.

        Returns:
            coeff_var (float)

        """
        stdev_mean = self.stddevs.mean()
        coeff_var = np.sqrt(np.sum(np.square(self.stddevs - stdev_mean)))
        coeff_var /= stdev_mean * (len(self.stddevs) - 1)
        return coeff_var

    @property
    def calibration_err(self) -> float:
        """Calculate the calibration error of the model.

        Calls :meth:`pis`, which is relatively slow.

        Returns:
            calibration_error (float)

        """
        predicted_pi, observed_pi = self.pis
        return np.sum(np.square(predicted_pi - observed_pi))

    @property
    def residuals(self) -> np.ndarray:
        """Calculate the residuals.

        Returns:
            residuals (:obj:`np.ndarray`): The difference between the means
                of the predicted distributions and the true values.

        """
        return self.mean - self.val_obs.numpy()

    def sharpness_plot(self, fname: Optional[Union[str, Path]] = None):
        """Plot the distribution of standard deviations and the sharpness.

        Args:
            fname (str or :obj:`Path`, optional): The name of the file to save to.
                If omitted, will show the plot after completion.

        """
        plot_sharpness(self.stddevs, self.sharpness, self.variation, fname)

    def calibration_plot(self, fname: Optional[Union[str, Path]] = None):
        """Plot the distribution of residuals relative to the expected distribution.

        Args:
            fname (str or :obj:`Path`, optional): The name of the file to save to.
                If omitted, will show the plot after completion.

        """
        predicted_pi, observed_pi = self.pis
        plot_calibration(predicted_pi, observed_pi, fname)

    @property
    def pis(self) -> Tuple[np.ndarray, np.ndarray]:
        """Calculate the percentile interval densities of a model.

        Based on the implementation by `Tran et al.`_. Initially proposed by `Kuleshov et al.`_.

        Args:
            residuals (:obj:`np.ndarray`): The normalised residuals of the model predictions.

        Returns:
            predicted_pi (:obj:`np.ndarray`): The percentiles used.
            observed_pi (:obj:`np.ndarray`): The density of residuals that fall within each of the
                `predicted_pi` percentiles.

        .. _Tran et al.:
            https://arxiv.org/abs/1912.10066
        .. _Kuleshov et al.:
            https://arxiv.org/abs/1807.00263

        """
        norm_resids = self.residuals / self.stddevs  # Normalise residuals

        norm = tfd.Normal(0, 1)  # Standard normal distribution

        predicted_pi = np.linspace(0, 1, 100)
        bounds = norm.quantile(
            predicted_pi
        ).numpy()  # Find the upper bounds for each percentile

        observed_pi = np.array(
            [np.count_nonzero(norm_resids <= bound) for bound in bounds]
        )  # The number of residuals that fall within each percentile
        observed_pi = (
            observed_pi / norm_resids.size
        )  # The fraction (density) of residuals that fall within each percentile

        return predicted_pi, observed_pi