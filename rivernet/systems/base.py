import pytorch_lightning as pl
from allennlp.common.registrable import Registrable


class System(Registrable, pl.LightningModule):
    pass
