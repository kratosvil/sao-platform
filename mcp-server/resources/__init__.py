from .base import ResourcePlugin
from .lambda_ import LambdaPlugin
from .ecs import ECSPlugin

RESOURCE_REGISTRY: dict[str, type[ResourcePlugin]] = {
    "AWS::Lambda::Function": LambdaPlugin,
    "AWS::ECS::Service": ECSPlugin,
}


def get_plugin(resource_type: str) -> type[ResourcePlugin] | None:
    return RESOURCE_REGISTRY.get(resource_type)
