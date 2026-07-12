# Codex 초기화권 자동 사용 관리자

Codex 초기화권 자동 사용 관리자는 선택된 Codex 초기화권 한 장을 만료 약 5분 전에 안전하게 사용하는 Windows 도구입니다. 설치 후 시작 메뉴의 작은 관리창에서 **Start Automatic Use**를 한 번 선택하면 됩니다.

영문 문서는 [README.md](README.md)에 있습니다.

## 빠른 시작

1. `setup.cmd`를 더블클릭합니다.
2. 설치가 끝나면 **Codex Reset Credit Manager** 창이 열립니다.
3. **Start Automatic Use**를 선택합니다.
4. 창에 **Automatic use: On**과 사용 예정 시각이 표시되는지 확인합니다.

Windows 시간이 `time.windows.com`과 동기화되어야 할 때만 설치기가 UAC 권한을 요청합니다. 시간 동기화가 이미 정상이면 UAC 창은 나타나지 않습니다.

## 관리창 닫기와 다시 열기

창의 **X**를 선택하면 프로그램을 종료하지 않고 알림 영역으로 숨깁니다. Tray 아이콘을 더블클릭하면 창이 다시 나타납니다. Tray 메뉴는 다음 항목을 제공합니다.

- **Open Manager**
- **Check Now**
- **Start Automatic Use** 또는 **Pause Automatic Use**
- **Exit UI**

**Exit UI**는 tray 아이콘과 관리창만 종료합니다. 자동 사용을 일시정지하거나 예약된 초기화권을 취소하거나 후속 예약을 중단하지 않습니다. UI를 다시 표시하려면 시작 메뉴에서 **Codex Reset Credit Manager**를 실행합니다. 이미 tray에서 실행 중이면 새 프로세스를 만들지 않고 기존 창을 복원합니다.

Tray 프로세스는 로그인할 때 자동으로 시작되지 않습니다. 관리창이나 tray 아이콘이 없어도 Windows 예약 작업이 자동운영을 계속하므로 처리에는 영향이 없습니다.

## 자동운영 방식

두 종류의 Windows 예약 작업이 역할을 나누어 수행합니다.

- `ManagerSync`는 로그인할 때와 30분마다 계정, Codex CLI, 시계, 초기화권 목록을 확인합니다. Policy와 one-shot 상태를 조정하며 one-shot 작업을 예약·교체·해제할 수 있지만 초기화권을 실제로 사용하지는 않습니다.
- exact-ID one-shot 작업은 해당 권한이 만료되기 약 5분 전에 실제 사용을 담당합니다. 다른 권한으로 대체할 수 없습니다.

30분 주기는 이미 예약된 권한의 정확한 T-5 처리 시각을 바꾸지 않습니다. 새 권한, CLI 업데이트, 복구 가능한 controller 상태를 발견하는 속도에만 영향을 줍니다.

새로 나타난 권한은 만료까지 약 **45분 45초 이상** 남아 있을 때만 자동 발견을 보장합니다. 이 시간에는 최대 30분의 동기화 대기, 10분의 안전한 등록 여유, 5분 45초의 pre-dispatch 준비 시간이 포함됩니다. 남은 시간이 더 짧으면 서두르거나 불명확한 live 작업을 만들지 않고 fail-closed합니다.

`ManagerSync`는 PC의 절전을 해제하지 않습니다. exact one-shot 작업만 `WakeToRun`을 유지합니다. 두 작업 모두 현재 사용자의 로그인 세션에서만 실행되며 해당 사용자가 로그오프한 동안에는 실행되지 않습니다.

## 평소 사용법

관리창에는 운영에 필요한 정보만 표시됩니다.

- 자동 사용: `On`, `Paused`, `Needs attention`
- 다음 권한 만료 및 사용 예정 시각
- Codex CLI 호환 상태와 Windows 시간 상태
- 마지막 처리 결과와 다음 예약 상태

**Check Now**는 안전 검증과 controller 상태 조정을 즉시 실행합니다. 검증된 상태에 따라 policy를 갱신하거나 one-shot 작업을 예약·교체·해제할 수 있지만 초기화권을 실제로 사용하지는 않습니다. **Pause Automatic Use**는 활성 예약에 취소 표시를 남기고 후속 권한이 예약되지 않도록 합니다.

Windows 알림은 사용 성공, no-action 또는 불명 결과, 호환성 차단, 계정·시간 문제, 새 예약을 알려줍니다. Windows가 알림을 표시하지 못하더라도 감사 로그와 관리창 상태가 최종 기준입니다.

## Codex CLI 업데이트

Codex CLI가 업데이트될 때마다 `revalidate-cli`를 실행하거나 다시 설치할 필요는 없습니다. `ManagerSync`가 전역 CLI 변경을 감지하고 다음 사항을 읽기 전용으로 검증합니다.

- 전역 npm 설치에 네이티브 Codex 바이너리가 정확히 하나인지
- 안정 버전 `0.144.1` 이상이며 package와 바이너리 버전이 일치하는지
- OpenAI Authenticode 서명이 유효한지
- app-server의 credit 상세 정보와 exact-ID consume 계약이 유지되는지
- 계정과 전체 초기화권 목록을 안전하게 읽을 수 있는지

호환되는 CLI는 향후 작업용으로 자동 승인됩니다. 현재 고정된 바이너리가 남아 있으면 이미 예약된 작업은 그 바이너리로 완료합니다. 호환성을 증명할 수 없거나 처리 시각이 너무 가까우면 추측하지 않고 해당 권한을 차단하거나 안전하게 놓칩니다.

## 설치와 업데이트

요구 사항:

- PowerShell 7(`pwsh.exe`)을 사용할 수 있는 Windows
- 같은 설치 폴더에 `python.exe`와 `pythonw.exe`가 있는 CPython 3.13
- 전역 npm으로 설치한 Codex CLI `0.144.1` 이상
- Codex CLI에 로그인된 계정

권장 설치 방법은 `setup.cmd` 더블클릭입니다. PowerShell에서 직접 실행하려면 다음 명령을 사용합니다.

```powershell
# 변경하지 않는 사전 확인
pwsh -NoProfile -File .\install.ps1 -WhatIf -Confirm:$false

# 설치 또는 업데이트
pwsh -NoProfile -File .\install.ps1 -Confirm:$false

# 필요한 경우 UAC를 이용한 시간 복구를 명시적으로 허용
pwsh -NoProfile -File .\install.ps1 -ConfigureWindowsTime -Confirm:$false
```

파일은 `%LOCALAPPDATA%\CodexResetCredit` 아래에 SHA-256 기반 불변 guard, manager, installer 경로로 설치됩니다. 영문 시작 메뉴 바로가기 이름은 **Codex Reset Credit Manager**입니다.

바로가기, `ManagerSync`, 새로 생성되는 모든 one-shot 작업은 `pythonw.exe`를 사용하므로 정상 운영 중 콘솔 창을 표시하지 않습니다. 이미 `ARMED`인 one-shot 작업은 실행 파일, action, 일정을 의도적으로 변경하지 않고 채택하므로 이전 버전에서 만든 작업은 한 번에 한해 `python.exe`를 계속 사용할 수 있습니다. `setup.cmd` 콘솔과 필요한 UAC 창은 의도적으로 표시됩니다.

업데이트는 기존의 켜짐 또는 일시정지 정책을 유지합니다. 또한 기존 `ARMED` 작업 하나를 중복 작업 생성 없이 채택합니다. 신규 설치는 **Start Automatic Use**를 선택할 때까지 일시정지 상태입니다.

### 다른 PC에 설치

위 요구 사항을 충족한 다음 소스 폴더 전체를 해당 PC로 복사하고 `setup.cmd`를 실행합니다. `%LOCALAPPDATA%\CodexResetCredit`이나 내보낸 Windows 예약 작업을 PC 사이에서 복사하면 안 됩니다. 각 PC에서 새로 검증하여 설치해야 합니다.

같은 Codex 계정에 대해 여러 PC의 자동 사용을 동시에 켜지 마십시오. 독립된 controller가 같은 권한을 발견해 서로 경쟁하는 예약을 만들 수 있습니다.

## 고급 명령

일반 사용에는 아래 명령이 필요하지 않습니다. 설치된 manager 경로는 시작 메뉴 바로가기 속성에서 확인할 수 있습니다.

```powershell
python .\codex_reset_manager.py ui
python .\codex_reset_manager.py enable
python .\codex_reset_manager.py pause
python .\codex_reset_manager.py sync --scheduled
python .\codex_reset_manager.py status --json
python .\codex_reset_manager.py doctor
```

Manifest 경로는 자동으로 탐색합니다. 내부 controller만 `install.ps1 -ManagerChildOnly -CodexPath <verified-codex.exe>`를 사용해 one-shot 작업을 정확히 하나 등록합니다.

## 안전 원칙

- 계정 조회와 소비는 로컬 Codex app-server만 사용합니다. `auth.json`을 읽거나 backend API를 직접 호출하지 않습니다.
- 새 v2 manifest, policy, 로그, UI, 알림에는 원본 `creditId`, 이메일 주소, 토큰, 멱등 키를 저장하거나 표시하지 않습니다. 기존 v1 manifest는 호환을 위해서만 유지합니다.
- 모든 consume 요청에는 미리 선택한 non-empty exact `creditId`와 프로세스 메모리에서 생성한 UUIDv4 멱등 키가 필요합니다. 같은 프로세스의 재전송만 동일 키를 재사용합니다.
- 불완전한 목록, 중복 ID, 최초 만료 동률, 계정 변경, 비호환 CLI, 잘못된 서명, 미동기화 시계, 변경된 작업 계약은 fail-closed합니다.
- 불명 결과 또는 `NO_ACTION` 대상은 만료되고 전체 목록에서 사라질 때까지 장벽으로 남습니다. 더 늦은 권한으로 건너뛰지 않습니다.
- UI를 숨기거나 종료해도 자동운영 정책은 바뀌지 않습니다. **Pause Automatic Use**나 `pause` 명령만 자동운영을 중지합니다.

## 테스트

자동 테스트는 fake app-server만 사용하며 실제 consume을 호출하지 않습니다.

```powershell
python -W error::ResourceWarning -m unittest discover -s tests -v
```
