from pydantic import BaseModel, ConfigDict, Field


class ConvertTemperature(BaseModel):
    model_config = ConfigDict(extra="forbid")
    celsius: float = Field(ge=-273.15, le=10000, allow_inf_nan=False)


async def convert_temperature(arguments):
    return {"celsius": arguments.celsius, "fahrenheit": arguments.celsius * 9 / 5 + 32}


def register(registry):
    registry.register(
        "convert_temperature",
        "Convert Celsius to Fahrenheit.",
        ConvertTemperature,
        convert_temperature,
    )
