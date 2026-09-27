from django.db import migrations, models

TAG_MAX_LENGTH = 40


def _free_tag(base_tag, hearth_id, model):
    """基于原牌号与主键生成不撞库的新牌号（长度受字段限制）。"""
    suffix = f"-复{hearth_id}"
    candidate = f"{base_tag[: TAG_MAX_LENGTH - len(suffix)]}{suffix}"
    n = 0
    while model.objects.filter(tag=candidate).exclude(id=hearth_id).exists():
        n += 1
        extra = f"-{n}"
        keep = TAG_MAX_LENGTH - len(suffix) - len(extra)
        candidate = f"{base_tag[:keep]}{suffix}{extra}"
    return candidate


def dedupe_hearth_tags(apps, schema_editor):
    """恢复唯一约束前，先把历史脏数据里的撞牌改名（保留最小 id 的原牌）。"""
    FireHearth = apps.get_model("kiln", "FireHearth")
    seen = set()
    for hearth in FireHearth.objects.order_by("id"):
        if hearth.tag not in seen:
            seen.add(hearth.tag)
            continue
        new_tag = _free_tag(hearth.tag, hearth.id, FireHearth)
        hearth.tag = new_tag
        hearth.save(update_fields=["tag"])
        seen.add(new_tag)


class Migration(migrations.Migration):

    dependencies = [
        ("kiln", "0002_firehearth_tag_drop_unique"),
    ]

    operations = [
        migrations.RunPython(dedupe_hearth_tags, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="firehearth",
            name="tag",
            field=models.CharField(max_length=40, unique=True, verbose_name="灶牌"),
        ),
    ]
