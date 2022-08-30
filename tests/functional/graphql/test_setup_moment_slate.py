import datetime
import random
import uuid
import time
from typing import Sequence

from fastapi.testclient import TestClient
from pytest import approx

from app.data_providers.corpus.corpus_feature_group_client import CorpusFeatureGroupClient
from app.data_providers.snowplow.config import SnowplowConfig
from app.data_providers.dispatch import SetupMomentDispatch
from app.data_providers.topic_provider import TopicProvider
from app.data_providers.user_recommendation_preferences_provider import UserRecommendationPreferencesProvider
from app.main import app
from app.models.corpus_item_model import CorpusItemModel
from app.models.user_ids import UserIds
from app.models.user_recommendation_preferences import UserRecommendationPreferencesModel
from tests.assets.topics import *
from tests.functional.test_dynamodb_base import TestDynamoDBBase

from unittest.mock import patch
from collections import namedtuple

from tests.functional.test_util.snowplow import SnowplowMicroClient

MockResponse = namedtuple('MockResponse', 'status')


corpus_topics = [health_topic, business_topic, entertainment_topic, technology_topic, gaming_topic, travel_topic]
corpus_topic_ids = [t.corpus_topic_id for t in corpus_topics]
topics_by_id = {t.id: t for t in corpus_topics}


def _user_recommendation_preferences_fixture(
        user_id: str, preferred_topics: List[TopicModel]
) -> UserRecommendationPreferencesModel:
    return UserRecommendationPreferencesModel(
        user_id=user_id,
        updated_at=datetime.datetime(2022, 5, 12, 15, 30),
        preferred_topics=preferred_topics,
    )


def _corpus_items_fixture(n: int) -> [CorpusItemModel]:
    return [CorpusItemModel(id=str(uuid.uuid4()), topic=random.choice(corpus_topic_ids)) for _ in range(n)]


def _get_topics_fixture(topics_ids: Sequence[str]) -> List[TopicModel]:
    return [topics_by_id[id] for id in topics_ids]


class TestSetupMomentSlate(TestDynamoDBBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.user_ids = UserIds(
            user_id=1,
            hashed_user_id='1-hashed',
        )

        self.snowplow_micro = SnowplowMicroClient(config=SnowplowConfig())
        self.snowplow_micro.reset_snowplow_events()

    @patch.object(CorpusFeatureGroupClient, 'get_corpus_items')
    @patch.object(UserRecommendationPreferencesProvider, 'fetch')
    def test_setup_moment_slate(self, mock_fetch_user_recommendation_preferences, mock_get_ranked_corpus_items):
        corpus_items_fixture = _corpus_items_fixture(n=100)
        mock_get_ranked_corpus_items.return_value = corpus_items_fixture

        preferred_topics = [technology_topic, gaming_topic]
        preferences_fixture = _user_recommendation_preferences_fixture(str(self.user_ids.user_id), preferred_topics)
        mock_fetch_user_recommendation_preferences.return_value = preferences_fixture

        with TestClient(app) as client:
            data = client.post(
                '/',
                json={
                    'query': '''
                        query SetupMomentSlate {
                          setupMomentSlate {
                            headline
                            subheadline
                            recommendations(count: 100) {
                              id
                              corpusItem {
                                id
                              }
                            }
                          }
                        }
                    ''',
                },
                headers={
                    'userId': str(self.user_ids.user_id),
                    'encodedId': self.user_ids.hashed_user_id,
                }
            ).json()

            assert not data.get('errors')
            slate = data['data']['setupMomentSlate']
            recs = slate['recommendations']
            assert slate['headline'] == 'Save an article you find interesting'

            # Assert that all corpus items are being returned.
            assert len(recs) == len(corpus_items_fixture)
            assert {rec['corpusItem']['id'] for rec in recs} == {item.id for item in corpus_items_fixture}

            self.validate_snowplow_event(expected_recommendation_count=len(corpus_items_fixture))

    @patch.object(CorpusFeatureGroupClient, 'get_corpus_items')
    @patch.object(UserRecommendationPreferencesProvider, 'fetch')
    @patch.object(TopicProvider, 'get_topics')
    def test_default_count(
            self, mock_get_topics, mock_fetch_user_recommendation_preferences, mock_get_ranked_corpus_items):
        corpus_items_fixture = _corpus_items_fixture(n=100)
        mock_get_ranked_corpus_items.return_value = corpus_items_fixture
        default_recommendation_count = 10  # Number of recommendations that is expected to be returned by default.

        mock_fetch_user_recommendation_preferences.return_value = \
            _user_recommendation_preferences_fixture(str(self.user_ids.user_id), [])
        mock_get_topics.return_value = _get_topics_fixture(SetupMomentDispatch.DEFAULT_TOPICS)

        with TestClient(app) as client:
            data = client.post(
                '/',
                json={
                    'query': '''
                        query SetupMomentSlate {
                          setupMomentSlate {
                            recommendations {
                              id
                              corpusItem {
                                id
                              }
                            }
                          }
                        }
                    ''',
                },
                headers={
                    'userId': str(self.user_ids.user_id),
                    'encodedId': self.user_ids.hashed_user_id,
                }
            ).json()

            assert not data.get('errors')
            slate = data['data']['setupMomentSlate']
            recs = slate['recommendations']

            # Assert that 10 (the default for count) corpus items are being returned.
            assert len(recs) == default_recommendation_count

            self.validate_snowplow_event(expected_recommendation_count=default_recommendation_count)

    def validate_snowplow_event(self, expected_recommendation_count: int):
        """
        Assert that slate metadata was sent to Snowplow Micro (hosted locally as a Docker service)

        :param expected_recommendation_count: Number of recommendations that is expected to be sent to Snowplow.
        """
        all_snowplow_events = self.snowplow_micro.get_event_counts()
        assert all_snowplow_events == {'total': 1, 'good': 1, 'bad': 0}, self.snowplow_micro.get_last_error()

        good_events = self.snowplow_micro.get_good_events()
        event_contexts = good_events[0]['contexts']
        assert SnowplowConfig.USER_SCHEMA in event_contexts
        assert SnowplowConfig.CORPUS_SLATE_SCHEMA in event_contexts

        # Assert that the context data matches the expected user_id and recommendation count.
        for context_data in good_events[0]['event']['contexts']['data']:
            if context_data['schema'] == SnowplowConfig.CORPUS_SLATE_SCHEMA:
                assert len(context_data['data']['recommendations']) == expected_recommendation_count
                # recommended_at is accurate to a minute.
                # (e.g. `datetime.utcnow().timestamp()` is off by 7 hours if your local time is PDT.)
                assert context_data['data']['recommended_at'] == approx(time.time(), rel=60)
            elif context_data['schema'] == SnowplowConfig.USER_SCHEMA:
                assert context_data['data']['user_id'] == int(self.user_ids.user_id)
