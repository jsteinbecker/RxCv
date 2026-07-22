from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from rxocrpl.models import RoleGrant

from .models import User


class RoleGrantInline(admin.TabularInline):
      model = RoleGrant
      extra = 0
      fk_name = "user"
      readonly_fields = ["granted_at"]
      fields = [
            "role",
            "organization",
            "facility",
            "granted_by",
            "reason",
            "granted_at",
            "revoked_at",
            "expires_at",
      ]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
      fieldsets = BaseUserAdmin.fieldsets + (
            ("Pharmacy Role", {"fields": ("is_rph", "facility", "user_type")}),
      )
      add_fieldsets = BaseUserAdmin.add_fieldsets + (
            ("Pharmacy Role", {"fields": ("is_rph", "facility", "user_type")}),
      )
      list_display = [
            "username",
            "email",
            "first_name",
            "last_name",
            "is_rph",
            "is_staff",
            "facility",
            "user_type",
      ]
      list_filter = BaseUserAdmin.list_filter + ("facility", "user_type")
      search_fields = ["username", "email", "first_name", "last_name"]
      raw_id_fields = ["facility"]
      inlines = [RoleGrantInline]
