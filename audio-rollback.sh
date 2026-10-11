#!/bin/bash
#
# audio-rollback.sh - обратимый откат фикса звука (CS35L41) для Lenovo Legion 7
# 16IAX7 (82TD). Ядро определяется АВТОМАТИЧЕСКИ.
#
# ===========================================================================
# КАК ПРИМЕНЯТЬ
#
#   sudo ./audio-rollback.sh
#       Найти ядра автоматически и откатить фикс во всех найденных.
#       Это обычный вариант.
#
#   sudo ./audio-rollback.sh 7.0.0-38-generic
#       Откатить только указанное ядро. Можно перечислить несколько ядер.
#
#   ./audio-rollback.sh --dry-run
#       ПОКАЗАТЬ, что будет сделано, и ничего не менять. Права root не нужны.
#       Рекомендуется запускать первым делом, чтобы посмотреть план.
#
#   sudo ./audio-rollback.sh --dir=/root/audio-fix-disabled
#       Каталог, куда складываются снятые файлы (по умолчанию именно этот).
#
#   ./audio-rollback.sh --help
#
# ЧТО ИМЕННО ДЕЛАЕТ (по шагам)
#   1. убирает патченные модули из /lib/modules/<ядро>/updates/
#   2. убирает симлинки прошивки 17aa3874 в /lib/firmware/cirrus/
#   3. снимает авто-хук /etc/kernel/postinst.d/zzz-cs35l41-legion
#      ВАЖНО: без шага 3 следующий апдейт ядра вернёт патч обратно
#   4. возвращает /etc/default/grub из бэкапа, если бэкап есть
#   5. depmod + update-grub + update-initramfs для затронутых ядер
#
# НИЧЕГО НЕ УДАЛЯЕТСЯ
#   Все файлы ПЕРЕМЕЩАЮТСЯ в каталог снятых файлов (шаги 1-3), поэтому откат
#   обратим в любой момент.
#
# КАК ВЕРНУТЬ ФИКС ОБРАТНО
#   Проще всего - запустить патчер (он положит модули заново):
#       sudo python3 cs35l41-legion-quirk.py <ядро>
#   Или вручную:
#       sudo cp -a /root/audio-fix-disabled/snd-hda-*.ko /lib/modules/<ядро>/updates/
#       sudo depmod -a <ядро>
#       sudo update-initramfs -u -k <ядро>
#   Плюс симлинки прошивки из /root/audio-fix-disabled обратно в /lib/firmware/cirrus/
#
# АВТООПРЕДЕЛЕНИЕ ЯДРА
#   Скрипт просматривает /lib/modules/<ядро>/updates/ и берёт те ядра, где
#   лежат наши модули (snd-hda-codec-alc269.ko, snd-hda-scodec-cs35l41.ko).
#   Если нигде не найдено - используется текущее ядро (uname -r).
#   Явный список ядер в аргументах отключает автоопределение.
#
# ПОСЛЕ ОТКАТА НУЖНА ПЕРЕЗАГРУЗКА.
# ===========================================================================
set -u

MODS="snd-hda-codec-alc269.ko snd-hda-scodec-cs35l41.ko"
FW_LINKS="cs35l41-dsp1-spk-prot-17aa3874-l0.bin.zst
cs35l41-dsp1-spk-prot-17aa3874-r0.bin.zst
cs35l41-dsp1-spk-prot-17aa3874.wmfw.zst"
FW_DIR=/lib/firmware/cirrus
HOOK=/etc/kernel/postinst.d/zzz-cs35l41-legion

DISABLED_DIR="${DISABLED_DIR:-/root/audio-fix-disabled}"
BACKUP_DIR="${BACKUP_DIR:-/root/audio-fix-backup}"
DRY=0
KERNELS=()
CHANGED=0

usage() {
  cat <<'EOT'
audio-rollback.sh - обратимый откат фикса звука CS35L41 (Lenovo Legion 7 16IAX7)

Использование:
  audio-rollback.sh [КЛЮЧИ] [ЯДРО ...]

Ключи:
  -n, --dry-run     показать план, ничего не менять (root не нужен)
      --dir=КАТАЛОГ куда складывать снятые файлы
                    (по умолчанию /root/audio-fix-disabled)
  -h, --help        эта справка

Примеры:
  ./audio-rollback.sh --dry-run              # посмотреть план
  sudo ./audio-rollback.sh                   # откатить во всех ядрах с фиксом
  sudo ./audio-rollback.sh 7.0.0-38-generic  # откатить только это ядро

Без аргументов-ядер ядро определяется автоматически: берутся все ядра, в
/lib/modules/<ядро>/updates/ которых лежат наши модули.
EOT
}

for a in "$@"; do
  case "$a" in
    -h|--help) usage; exit 0 ;;
    -n|--dry-run) DRY=1 ;;
    --dir=*) DISABLED_DIR="${a#--dir=}" ;;
    -*) echo "Неизвестный ключ: $a" >&2; usage; exit 2 ;;
    *) KERNELS+=("$a") ;;
  esac
done

if [ "$DRY" = 0 ] && [ "$UID" -ne 0 ]; then
  echo "ОШИБКА: нужны права root. Запустите через sudo, либо добавьте --dry-run." >&2
  exit 1
fi

echo "=== audio-rollback.sh ==="
[ "$DRY" = 1 ] && echo "РЕЖИМ: dry-run (ничего не меняется)"
echo

# ---- автоопределение ядер -------------------------------------------------
if [ "${#KERNELS[@]}" -eq 0 ]; then
  for u in /lib/modules/*/updates; do
    [ -d "$u" ] || continue
    hit=0
    for m in $MODS; do
      [ -e "$u/$m" ] && hit=1
    done
    [ "$hit" = 1 ] || continue
    k="${u#/lib/modules/}"
    KERNELS+=("${k%/updates}")
  done
  if [ "${#KERNELS[@]}" -gt 0 ]; then
    echo "Автоопределение: найдены ядра с фиксом: ${KERNELS[*]}"
  else
    IFS= read -r cur < /proc/sys/kernel/osrelease
    KERNELS+=("$cur")
    echo "Автоопределение: фикс в updates/ не найден, беру текущее ядро: $cur"
  fi
else
  echo "Ядра заданы вручную: ${KERNELS[*]}"
fi
echo

# ---- 1. патченные модули из updates/ --------------------------------------
echo "==> 1. Патченные модули из updates/"
for k in "${KERNELS[@]}"; do
  u=/lib/modules/$k/updates
  if [ ! -d "$u" ]; then
    echo "    [$k] нет каталога updates/ - пропуск"
    continue
  fi
  for m in $MODS; do
    [ -e "$u/$m" ] || continue
    echo "    [$k] $u/$m"
    if [ "$DRY" = 0 ]; then
      mkdir -p "$DISABLED_DIR"
      mv -v "$u/$m" "$DISABLED_DIR/$m"
    fi
    CHANGED=1
  done
done

# ---- 2. симлинки прошивки -------------------------------------------------
echo "==> 2. Симлинки прошивки 17aa3874 в $FW_DIR"
for f in $FW_LINKS; do
  p=$FW_DIR/$f
  [ -L "$p" ] || continue
  echo "    $p"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$DISABLED_DIR"
    mv -v "$p" "$DISABLED_DIR/$f"
  fi
  CHANGED=1
done

# ---- 3. авто-хук ----------------------------------------------------------
echo "==> 3. Авто-хук (без этого следующий апдейт ядра вернёт патч)"
if [ -e "$HOOK" ]; then
  echo "    $HOOK"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$DISABLED_DIR"
    mv -v "$HOOK" "$DISABLED_DIR/zzz-cs35l41-legion.disabled"
  fi
  CHANGED=1
else
  echo "    хука нет - пропуск"
fi

# ---- 4. GRUB --------------------------------------------------------------
echo "==> 4. Возврат стандартного /etc/default/grub"
if [ -f "$BACKUP_DIR/grub.default.bak" ]; then
  echo "    $BACKUP_DIR/grub.default.bak -> /etc/default/grub"
  if [ "$DRY" = 0 ]; then
    cp -a "$BACKUP_DIR/grub.default.bak" /etc/default/grub
  fi
  CHANGED=1
else
  echo "    бэкапа нет ($BACKUP_DIR/grub.default.bak) - пропуск"
fi

# ---- 5. depmod / grub / initramfs ----------------------------------------
echo "==> 5. depmod / update-grub / update-initramfs"
if [ "$CHANGED" = 0 ]; then
  echo "    изменений не было - ничего не пересобираю"
else
  for k in "${KERNELS[@]}"; do
    if [ ! -d "/lib/modules/$k" ]; then
      echo "    [$k] нет каталога /lib/modules/$k - пропуск"
      continue
    fi
    if [ "$DRY" = 1 ]; then
      echo "    [dry-run] depmod -a $k"
      echo "    [dry-run] update-initramfs -u -k $k"
    else
      echo "    depmod -a $k"
      depmod -a "$k" || echo "    (depmod вернул ошибку)"
      echo "    update-initramfs -u -k $k"
      update-initramfs -u -k "$k" || echo "    (update-initramfs вернул ошибку)"
    fi
  done
  if [ "$DRY" = 1 ]; then
    echo "    [dry-run] update-grub"
  elif command -v update-grub >/dev/null; then
    echo "    update-grub"
    update-grub >/dev/null 2>&1 || echo "    (update-grub вернул ошибку)"
  fi
fi

# ---- итог -----------------------------------------------------------------
echo
if [ "$DRY" = 1 ]; then
  echo "==> Это был dry-run. Ничего не изменено. Запустите без --dry-run (через sudo)."
else
  echo "==> Готово. Затронутые ядра: ${KERNELS[*]}"
  echo "    Снятые файлы: $DISABLED_DIR"
  echo "    Нужна перезагрузка."
  echo "    Вернуть фикс: sudo python3 cs35l41-legion-quirk.py <ядро>"
fi
