# Проверяет, от какого момента начинается предел ожидания задания. Если считать от фактического
# запуска модели, стоявшая в очереди работа могла бы выглядеть активной слишком долго.

from app.Http.Resources.Mobile.messages import phase


# Ставит время запуска почти в конец срока и проверяет подпись непосредственно до и после границы.
# Готовый результат должен показываться и после истечения срока.
def test_deadline_uses_original_acceptance_time_invariant():
    job = {"status": "running", "created_at": 1000, "started_at": 1299}
    assert phase(job, 1299.9) == "Генерация"
    assert phase(job, 1300) == "Время вышло"
    assert phase({**job, "status": "succeeded"}, 1400) == "Результат готов"
