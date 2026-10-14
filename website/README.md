# 문서 사이트 관리와 배포

사이트 원본은 `dev` 브랜치의 `website/`에서 관리한다. `.github/workflows/docs.yml`은
`dev`에 `website/**` 또는 워크플로 변경이 push되면 MkDocs로 빌드하고, 생성된 파일만
`gh-pages` 브랜치 루트에 커밋·push한다. GitHub Pages는 이 브랜치를 게시한다.
`main`은 이 워크플로의 자동 배포 대상이 아니다.

- `docs/`: 페이지 Markdown과 이미지
- `mkdocs.yml`: 메뉴와 테마 설정
- `site/`: 로컬 빌드 결과. 소스 브랜치에서는 Git 추적 대상에서 제외한다.
- `gh-pages` 브랜치: HTML·CSS·JavaScript·이미지와 `.nojekyll` 등 게시 파일만 보관한다.
  직접 편집하거나 `dev`에 병합하지 않는다. 다음 배포 시 빌드 결과로 갱신된다.

## 최초 설정

1. 이 저장소만 선택한 fine-grained personal access token을 만들고 저장소 권한
   `Contents: Read and write`를 부여한다. 조직의 승인이 필요하면 승인도 완료한다.
2. 저장소 **Settings → Secrets and variables → Actions**에 `GH_PAGES_TOKEN`이라는
   repository secret으로 등록한다. 토큰 값은 코드나 문서에 기록하지 않는다.
   토큰이 만료되면 이 Secret을 갱신한다.
3. 워크플로 변경을 `dev`에 반영한다. 첫 실행이 성공하면 `gh-pages`가 자동 생성된다.
   수동 실행 시에도 `dev`를 선택한다. 수동 실행 목록에 보이려면 워크플로가 저장소의
   기본 브랜치에도 있어야 한다.
4. **Settings → Pages → Build and deployment**에서 다음 값을 선택하고 저장한다.
   - Source: **Deploy from a branch**
   - Branch: **gh-pages**
   - Folder: **/(root)**
5. Actions에서 브랜치 갱신과 GitHub의 Pages 빌드·배포가 모두 성공했는지 확인한다.

기본 `GITHUB_TOKEN`으로 push한 커밋은 브랜치 기반 Pages 빌드를 시작하지 않으므로
별도 토큰을 사용한다. Secret이 없으면 워크플로는 명시적인 오류로 중단된다.
브랜치 보호 규칙이 있다면 배포 토큰 소유자가 `gh-pages`를 갱신할 수 있어야 한다.
Pages 사용 가능 여부는 저장소 공개 범위와 GitHub 요금제에 따른다.

## 수정과 로컬 확인

문서·이미지·메뉴를 수정한 뒤 `dev`에 반영하면 같은 사이트가 갱신된다.
빌드가 실패하면 `gh-pages`에는 변경이 올라가지 않는다.

저장소 루트에서 문서용 Python 환경을 준비한 뒤 다음과 같이 확인한다.

```sh
python -m pip install mkdocs-material
python -m mkdocs build --strict --config-file website/mkdocs.yml
```

최초 브랜치를 로컬에서 생성하거나 직접 배포해야 할 때는, 게시할 소스 변경을 먼저
커밋한 뒤 저장소 쓰기 권한이 있는 Git 인증으로 다음 명령을 실행한다.
이 명령은 `gh-pages`에 커밋하고 원격에 push하므로 실제 배포 작업이다.

```sh
python -m mkdocs gh-deploy --strict --config-file website/mkdocs.yml --remote-branch gh-pages
```

참고: [GitHub Pages 게시 소스 설정](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site),
[MkDocs 배포 안내](https://www.mkdocs.org/user-guide/deploying-your-docs/).
