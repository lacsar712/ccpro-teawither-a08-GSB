from django.contrib import admin

from .models import Garden, Trough, UnloadHandoff, WitherBatch


@admin.register(Garden)
class GardenAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "altitudeBand")
    search_fields = ("name", "altitudeBand")


@admin.register(Trough)
class TroughAdmin(admin.ModelAdmin):
    list_display = ("id", "garden", "troughCode", "cultivar", "loadKg", "status")
    list_filter = ("status", "garden")
    search_fields = ("troughCode", "cultivar")


@admin.register(WitherBatch)
class WitherBatchAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "trough",
        "startedAt",
        "targetMoisture",
        "actualMoisture",
        "rollGrade",
    )
    list_filter = ("rollGrade",)


@admin.register(UnloadHandoff)
class UnloadHandoffAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "trough",
        "handoffAt",
        "receivingTeam",
        "outKg",
        "signer",
        "completedAt",
    )
    list_filter = ("receivingTeam", "completedAt")
    search_fields = ("trough__troughCode", "receivingTeam", "signer")
