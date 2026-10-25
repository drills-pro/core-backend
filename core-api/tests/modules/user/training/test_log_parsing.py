from app.modules.user.training import log_parsing

LOGS = 'metric:acc=0.9\nmetric:bad=x\nmetrics_json:{"loss": [1]}\nmodel:{"w": [1]}\n'


def test_parsers_read_their_lines_from_a_job_log():
    assert log_parsing.parse_metrics(LOGS) == {"acc": 0.9}
    assert log_parsing.parse_extra_metrics(LOGS) == {"loss": [1]}
    assert log_parsing.parse_model_artifact(LOGS) == {"w": [1]}
    assert log_parsing.parse_model_artifact("nothing") is None
