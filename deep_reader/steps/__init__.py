from deep_reader.steps import extract, connect, annotate, synthesize, predict, calibrate, consolidate


def safe_format(template: str, **kwargs: str) -> str:
    """Replace {key} placeholders without str.format() — safe with arbitrary text."""
    result = template
    for key, val in kwargs.items():
        result = result.replace("{" + key + "}", str(val))
    return result
