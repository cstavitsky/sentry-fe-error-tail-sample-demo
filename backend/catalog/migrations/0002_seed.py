"""Seed generic demo data (no real customer data)."""

from django.db import migrations

MODELS = [
    ("text-summarizer", "Text Summarizer", "pytorch"),
    ("image-tagger", "Image Tagger", "onnx"),
    ("speech-to-text", "Speech to Text", "tensorrt"),
]
STATES = ["BUILDING", "DEPLOYING", "ACTIVE", "ACTIVE", "SCALED_TO_ZERO", "ACTIVE"]


def seed(apps, schema_editor):
    Model = apps.get_model("catalog", "Model")
    ModelVersion = apps.get_model("catalog", "ModelVersion")
    DeploymentStatus = apps.get_model("catalog", "DeploymentStatus")
    for slug, name, framework in MODELS:
        model = Model.objects.create(slug=slug, name=name, framework=framework)
        for v in (1, 2, 3):
            config = '{"gpu": "A10G", "min_replicas": 0, "max_replicas": 4}'
            if slug == "speech-to-text" and v == 3:
                config = '{"gpu": "A10G", "min_replicas": 0,'  # deliberately broken (scenario S3)
            version = ModelVersion.objects.create(model=model, version=v, config=config)
            for i, state in enumerate(STATES):
                DeploymentStatus.objects.create(
                    model_version=version, state=state, replicas=i % 4, message=f"{state.lower()} (seed)"
                )


def unseed(apps, schema_editor):
    apps.get_model("catalog", "Model").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
