from django.contrib import admin

from .models import Activity, AgentConfig, Lead, Market, Message, Suppression


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("kind", "status", "subject", "sent_at", "classification")
    readonly_fields = fields


@admin.register(Market)
class MarketAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "currency", "calling_code", "timezone")
    list_editable = ("enabled",)


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = ("name", "market", "city", "category", "email", "status", "fit_score", "updated_at")
    list_filter = ("status", "market", "category")
    search_fields = ("name", "email", "website")
    inlines = [MessageInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("lead", "kind", "direction", "status", "subject", "created_at")
    list_filter = ("status", "kind", "direction")


admin.site.register(AgentConfig)
admin.site.register(Suppression)
admin.site.register(Activity)
