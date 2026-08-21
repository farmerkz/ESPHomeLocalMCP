#!/usr/bin/env python3
"""
Скрипт управления и повышения версий (Version Bumper) для ESPHome Local MCP Server.
Поддерживает SemVer 2.0.0, синхронизацию с __version__.py, CHANGELOG.md и README.md.

Использование:
    python3 scripts/bump_version.py patch       # 1.0.0 -> 1.0.1
    python3 scripts/bump_version.py minor       # 1.0.0 -> 1.1.0
    python3 scripts/bump_version.py major       # 1.0.0 -> 2.0.0
    python3 scripts/bump_version.py 1.2.3       # Установка конкретной версии

Опции:
    --dry-run      Показать планируемые изменения без записи файлов
    --tag          Создать git-тег vX.Y.Z после бампа
    --no-commit    Не создавать git-коммит автоматически
"""

import argparse
import datetime
import os
import re
import subprocess
import sys

SEMVER_REGEX = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(PROJECT_ROOT, "__version__.py")
CHANGELOG_FILE = os.path.join(PROJECT_ROOT, "CHANGELOG.md")
README_FILE = os.path.join(PROJECT_ROOT, "README.md")


def get_current_version() -> str:
    """Извлекает текущую версию из __version__.py."""
    if not os.path.isfile(VERSION_FILE):
        raise FileNotFoundError(f"Файл {VERSION_FILE} не найден.")
    with open(VERSION_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
    if not match:
        raise ValueError(f"Не удалось найти __version__ в {VERSION_FILE}")
    return match.group(1)


def calculate_next_version(current: str, bump_type: str) -> str:
    """Вычисляет следующую версию по типу бампа (patch, minor, major) или возвращает заданную."""
    match = SEMVER_REGEX.match(current)
    if not match:
        raise ValueError(f"Текущая версия '{current}' не соответствует формату SemVer.")

    major = int(match.group("major"))
    minor = int(match.group("minor"))
    patch = int(match.group("patch"))

    bump_lower = bump_type.lower()
    if bump_lower == "patch":
        return f"{major}.{minor}.{patch + 1}"
    elif bump_lower == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_lower == "major":
        return f"{major + 1}.0.0"
    else:
        # Проверяем, передана ли корректная конкретная версия
        explicit_match = SEMVER_REGEX.match(bump_type)
        if not explicit_match:
            raise ValueError(
                f"Некорректный тип бампа или версия: '{bump_type}'. "
                f"Допустимы: patch, minor, major или строка версии X.Y.Z."
            )
        return bump_type


def update_version_py(new_version: str, dry_run: bool = False) -> None:
    """Обновляет __version__.py."""
    parts = new_version.split("-")[0].split("+")[0].split(".")
    version_info = f"({', '.join(parts)})"

    new_content = (
        f'"""\n'
        f'ESPHome Local MCP Server Version Information.\n'
        f'Single Source of Truth (SSOT) для версионирования проекта.\n'
        f'"""\n\n'
        f'__version__ = "{new_version}"\n'
        f'__version_info__ = {version_info}\n'
    )

    if dry_run:
        print(f"[DRY-RUN] Обновление {VERSION_FILE}:\n{new_content}")
    else:
        with open(VERSION_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"✅ Обновлен {VERSION_FILE} -> v{new_version}")


def update_readme(new_version: str, dry_run: bool = False) -> None:
    """Обновляет бейдж версии в README.md."""
    if not os.path.isfile(README_FILE):
        return

    with open(README_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # Заменяем бейдж версии: version-X.Y.Z-blue.svg
    updated = re.sub(
        r'badge/version-[^-\s]+-blue\.svg',
        f'badge/version-{new_version}-blue.svg',
        content
    )

    if dry_run:
        if updated != content:
            print(f"[DRY-RUN] Бейдж версии в {README_FILE} будет обновлен до {new_version}")
    else:
        with open(README_FILE, "w", encoding="utf-8") as f:
            f.write(updated)
        print(f"✅ Обновлен бейдж версии в {README_FILE}")


def update_changelog(new_version: str, dry_run: bool = False) -> None:
    """Переносит записи из [Unreleased] в новую секцию [X.Y.Z] - YYYY-MM-DD в CHANGELOG.md."""
    if not os.path.isfile(CHANGELOG_FILE):
        return

    with open(CHANGELOG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    today = datetime.date.today().isoformat()
    unreleased_pattern = r'## \[Unreleased\](.*?)(?=\n## \[|\Z)'
    match = re.search(unreleased_pattern, content, re.DOTALL)

    if match:
        unreleased_body = match.group(1).strip()
        if not unreleased_body:
            unreleased_body = "### Changed\n- Обновление версии проекта."

        replacement = (
            f"## [Unreleased]\n\n"
            f"---\n\n"
            f"## [{new_version}] - {today}\n\n"
            f"{unreleased_body}"
        )
        updated_content = content[:match.start()] + replacement + content[match.end():]
    else:
        # Если секция Unreleased не найдена, вставляем сверху
        header = f"# 📝 Журнал изменений (Changelog)\n\n## [Unreleased]\n\n---\n\n## [{new_version}] - {today}\n"
        updated_content = content.replace("# 📝 Журнал изменений (Changelog)\n", header)

    if dry_run:
        print(f"[DRY-RUN] Секция [Unreleased] в {CHANGELOG_FILE} будет преобразована в [{new_version}] - {today}")
    else:
        with open(CHANGELOG_FILE, "w", encoding="utf-8") as f:
            f.write(updated_content)
        print(f"✅ Обновлен {CHANGELOG_FILE} (добавлена секция [{new_version}] - {today})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Управление версионированием проекта ESPHome Local MCP.")
    parser.add_argument(
        "bump",
        help="Тип повышения версии (patch, minor, major) или явный номер версии (например 1.2.0)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать изменения без сохранения")
    parser.add_argument("--tag", action="store_true", help="Создать git-тег vX.Y.Z")
    parser.add_argument("--no-commit", action="store_true", help="Не выполнять git commit")

    args = parser.parse_args()

    current_ver = get_current_version()
    next_ver = calculate_next_version(current_ver, args.bump)

    print(f"Текущая версия: v{current_ver}")
    print(f"Новая версия:   v{next_ver}")

    if current_ver == next_ver:
        print("⚠️ Новая версия совпадает с текущей. Изменения не требуются.")
        return

    update_version_py(next_ver, dry_run=args.dry_run)
    update_readme(next_ver, dry_run=args.dry_run)
    update_changelog(next_ver, dry_run=args.dry_run)

    if not args.dry_run and not args.no_commit:
        try:
            subprocess.run(["git", "add", "__version__.py", "CHANGELOG.md", "README.md"], cwd=PROJECT_ROOT, check=True)
            commit_msg = f"chore(release): bump version to v{next_ver}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=PROJECT_ROOT, check=True)
            print(f"✅ Создан коммит: '{commit_msg}'")

            if args.tag:
                tag_name = f"v{next_ver}"
                subprocess.run(["git", "tag", "-a", tag_name, "-m", f"Release {tag_name}"], cwd=PROJECT_ROOT, check=True)
                print(f"✅ Создан git-тег: {tag_name}")
        except subprocess.CalledProcessError as e:
            print(f"⚠️ Ошибка выполнения git-команды: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
