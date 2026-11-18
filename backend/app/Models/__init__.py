# Собирает таблицы в общую схему и открывает их имена остальному серверу. Импорт должен загрузить
# все таблицы, чтобы создание и обновление базы видели полный набор связей.

"""Загружает модели всех таблиц в общую схему SQLAlchemy."""

from app.Models.accounts import AccountErasure as AccountErasure
from app.Models.accounts import AuthAttempt as AuthAttempt
from app.Models.accounts import ErasedSession as ErasedSession
from app.Models.accounts import PasswordReset as PasswordReset
from app.Models.accounts import RegistrationCode as RegistrationCode
from app.Models.accounts import Session as Session
from app.Models.accounts import User as User
from app.Models.base import Base as Base
from app.Models.chats import ChatSource as ChatSource
from app.Models.chats import Thread as Thread
from app.Models.generation import ACTIVE_JOB_PREDICATE as ACTIVE_JOB_PREDICATE
from app.Models.generation import ErasedGeneration as ErasedGeneration
from app.Models.generation import GenerationReport as GenerationReport
from app.Models.generation import Job as Job
from app.Models.generation import JobContentRemoval as JobContentRemoval
from app.Models.generation import PendingGpuRemoval as PendingGpuRemoval
from app.Models.generation import ResultRemoval as ResultRemoval
from app.Models.media import Asset as Asset
from app.Models.media import AssetDerivative as AssetDerivative
from app.Models.media import PendingMediaRemoval as PendingMediaRemoval
from app.Models.media import Transfer as Transfer
from app.Models.media import UploadGrant as UploadGrant
