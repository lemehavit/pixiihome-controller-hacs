"""Button availability follows control authority after start-up."""

from types import SimpleNamespace

from custom_components.pixiihome_controller.button import (
    BUTTON_DESCRIPTIONS,
    PixiiNativeButton,
)


def test_button_availability_follows_authority_through_polling() -> None:
    authority = {"enabled": False}

    async def execute(*_args):
        return None

    runtime = SimpleNamespace(
        serial="GATEWAY",
        async_execute_native=execute,
        production_control_enabled=lambda: authority["enabled"],
    )
    button = PixiiNativeButton(runtime, BUTTON_DESCRIPTIONS[0])

    # Home Assistant buttons do not poll by default; without polling the
    # availability seen at start-up (authority held off) would never change.
    assert button.should_poll is True
    assert button.available is False
    authority["enabled"] = True
    assert button.available is True
