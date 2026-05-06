from django.contrib import admin

from .models import (
    PersonalBudget,
    PersonalCategory,
    PersonalExpense,
    PersonalExpenseTemplate,
    PersonalFinanceSettings,
    PersonalIncome,
)


@admin.register(PersonalCategory)
class PersonalCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "icon", "color")
    list_filter = ("user",)
    search_fields = ("name",)


@admin.register(PersonalExpense)
class PersonalExpenseAdmin(admin.ModelAdmin):
    list_display = ("date", "user", "category", "item_name", "amount", "payment_method")
    list_filter = ("user", "category", "payment_method")
    search_fields = ("item_name", "description")
    date_hierarchy = "date"


@admin.register(PersonalIncome)
class PersonalIncomeAdmin(admin.ModelAdmin):
    list_display = ("date", "user", "source", "amount")
    list_filter = ("user",)
    search_fields = ("source", "description")
    date_hierarchy = "date"


@admin.register(PersonalBudget)
class PersonalBudgetAdmin(admin.ModelAdmin):
    list_display = ("user", "category", "monthly_limit")
    list_filter = ("user",)


@admin.register(PersonalExpenseTemplate)
class PersonalExpenseTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "category", "amount", "payment_method")
    list_filter = ("user",)
    search_fields = ("name",)


@admin.register(PersonalFinanceSettings)
class PersonalFinanceSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "currency_code", "billing_start_day", "updated_at")
    list_filter = ("currency_code",)
