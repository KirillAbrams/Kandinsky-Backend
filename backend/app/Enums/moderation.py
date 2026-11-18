# Хранит общий текст отказа, причины жалоб и возможные решения. Одинаковые значения нужны серверу
# и обработчику, чтобы жалоба и итог отображались согласованно.

from typing import Literal

ReportReason = Literal["inappropriate", "harmful", "other"]
ReportDecision = Literal["keep", "remove", "remove-and-suspend"]
BLOCKED_GENERATION = "Генерация заблокирована после проверки"
