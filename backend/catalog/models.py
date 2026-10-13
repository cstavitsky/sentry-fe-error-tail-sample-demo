from django.db import models


class Model(models.Model):
    """An ML model that customers deploy."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    framework = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.slug


class ModelVersion(models.Model):
    model = models.ForeignKey(Model, related_name="versions", on_delete=models.CASCADE)
    version = models.PositiveIntegerField()
    # JSON text; one seeded row is deliberately malformed for scenario S3.
    config = models.TextField(default="{}")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("model", "version")]


class DeploymentStatus(models.Model):
    class State(models.TextChoices):
        BUILDING = "BUILDING"
        DEPLOYING = "DEPLOYING"
        ACTIVE = "ACTIVE"
        FAILED = "FAILED"
        SCALED_TO_ZERO = "SCALED_TO_ZERO"

    model_version = models.ForeignKey(ModelVersion, related_name="statuses", on_delete=models.CASCADE)
    state = models.CharField(max_length=20, choices=State.choices)
    replicas = models.PositiveIntegerField(default=0)
    message = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
