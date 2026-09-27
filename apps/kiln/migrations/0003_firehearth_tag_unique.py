from django.db import migrations, models


TAG_MAX_LENGTH = 40


def deduplicate_hearth_tags(apps, schema_editor):
    """加唯一约束前，给存量重名灶牌逐个改名去重。

    每组重牌保留最小 id 的原名，其余按自身 id 改名为「原名-重<id>」；
    候选名仍撞库时继续追加序号。改名稳定可复算，不依赖随机数。
    """
    FireHearth = apps.get_model("kiln", "FireHearth")

    first_seen = {}
    duplicates = []
    for pk, tag in FireHearth.objects.order_by("id").values_list("id", "tag"):
        if tag in first_seen:
            duplicates.append((pk, tag))
        else:
            first_seen[tag] = pk

    if not duplicates:
        return

    used = set(first_seen.keys())

    for pk, tag in duplicates:
        suffix = f"-重{pk}"
        base = tag[: TAG_MAX_LENGTH - len(suffix)]
        candidate = base + suffix
        seq = 2
        while candidate in used:
            extra = f"-{seq}"
            candidate = (tag[: TAG_MAX_LENGTH - len(suffix) - len(extra)] + suffix + extra)
            seq += 1
        used.add(candidate)
        FireHearth.objects.filter(id=pk).update(tag=candidate)


class Migration(migrations.Migration):

    dependencies = [
        ("kiln", "0002_firehearth_tag_drop_unique"),
    ]

    operations = [
        migrations.RunPython(
            deduplicate_hearth_tags,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="firehearth",
            name="tag",
            field=models.CharField(max_length=40, unique=True, verbose_name="灶牌"),
        ),
    ]
