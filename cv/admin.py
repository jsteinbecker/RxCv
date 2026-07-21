from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
      fieldsets = BaseUserAdmin.fieldsets + (
            ("Pharmacy Role", {"fields": ("is_rph",)}),
      )
      add_fieldsets = BaseUserAdmin.add_fieldsets + (
            ("Pharmacy Role", {"fields": ("is_rph",)}),
      )
      list_display = [
            "username",
            "email",
            "first_name",
            "last_name",
            "is_rph",
            "is_staff",
            "facility_assignment",
      ]
      search_fields = ["username", "email", "first_name", "last_name"]

      @admin.display(description="Facility")
      def facility_assignment (self, obj):
            try:
                  profile = obj.facility_profile
                  return profile.facility
            except AttributeError:
                  return "—"
