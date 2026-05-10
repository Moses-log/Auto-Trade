import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("WEBHOOK_SECRET", "MY_SHARED_SECRET")

CONTENT = '{"investors": []}'


@pytest.mark.asyncio
async def test_commit_raises_when_github_token_not_set():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = None
        from app.github_commit import commit_investors_json
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
            await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_raises_on_get_failure():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=MagicMock(status_code=401, text="Unauthorized"))
            from app.github_commit import commit_investors_json
            with pytest.raises(RuntimeError, match="GitHub GET failed"):
                await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_raises_on_put_failure():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_resp = MagicMock(status_code=200)
            get_resp.json = MagicMock(return_value={"sha": "abc123"})
            mock_client.get = AsyncMock(return_value=get_resp)
            mock_client.put = AsyncMock(return_value=MagicMock(status_code=422, text="Unprocessable"))
            from app.github_commit import commit_investors_json
            with pytest.raises(RuntimeError, match="GitHub PUT failed"):
                await commit_investors_json(CONTENT)


@pytest.mark.asyncio
async def test_commit_succeeds_and_calls_put_with_base64_content():
    with patch("app.github_commit.settings") as mock_settings:
        mock_settings.github_token = "fake-token"
        mock_settings.github_repo = "Moses-log/Auto-Trade"
        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            get_resp = MagicMock(status_code=200)
            get_resp.json = MagicMock(return_value={"sha": "abc123"})
            mock_client.get = AsyncMock(return_value=get_resp)
            mock_client.put = AsyncMock(return_value=MagicMock(status_code=201, text="Created"))
            from app.github_commit import commit_investors_json
            await commit_investors_json(CONTENT)
        mock_client.put.assert_called_once()
        put_kwargs = mock_client.put.call_args[1]
        import base64
        assert put_kwargs["json"]["sha"] == "abc123"
        assert put_kwargs["json"]["content"] == base64.b64encode(CONTENT.encode()).decode()
