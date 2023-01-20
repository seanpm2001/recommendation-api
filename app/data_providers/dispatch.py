import functools
import random
from asyncio import gather
from typing import List, Coroutine, Any

from aws_xray_sdk.core import xray_recorder
from dependency_injector.wiring import Provide, inject

from app.config import DEFAULT_TOPICS, GERMAN_HOME_TOPICS
from app.data_providers.item2item import Item2ItemRecommender, Item2ItemError, QdrantError
from app.data_providers.corpus.corpus_feature_group_client import CorpusFeatureGroupClient
from app.data_providers.slate_providers.collection_slate_provider import CollectionSlateProvider
from app.data_providers.slate_providers.for_you_slate_provider import ForYouSlateProvider
from app.data_providers.slate_providers.life_hacks_slate_provider import LifeHacksSlateProvider
from app.data_providers.slate_providers.pocket_hits_slate_provider import PocketHitsSlateProvider
from app.data_providers.slate_providers.recommended_reads_slate_provider import RecommendedReadsSlateProvider
from app.data_providers.slate_providers.topic_slate_provider_factory import TopicSlateProviderFactory
from app.data_providers.topic_provider import TopicProvider
from app.data_providers.unleash_provider import UnleashProvider
from app.data_providers.user_impression_cap_provider import UserImpressionCapProvider
from app.data_providers.user_recommendation_preferences_provider import UserRecommendationPreferencesProvider
from app.models.corpus_recommendation_model import CorpusRecommendationModel
from app.models.corpus_slate_lineup_model import CorpusSlateLineupModel, RecommendationSurfaceId
from app.models.corpus_slate_model import CorpusSlateModel
from app.models.localemodel import LocaleModel
from app.models.topic import TopicModel
from app.models.request_user import RequestUser
from app.singletons import Container


def _empty_on_error(func):
    """ Fallback to empty recommendations if Qdrant error occurred """
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except QdrantError:
            return []
    return wrapper


class Item2ItemDispatch:
    def __init__(self, item_recommender: Item2ItemRecommender):
        self.item_recommender = item_recommender

    async def after_save(self, resolved_id: int, count: int) -> List[CorpusRecommendationModel]:
        try:
            recs = await self.item_recommender.related(resolved_id, count)
        except Item2ItemError:
            # do not fallback for "Similar stores" after saving
            recs = []
        return self._sample(recs, count)

    @_empty_on_error
    async def after_article(self, resolved_id: int, count: int) -> List[CorpusRecommendationModel]:
        try:
            recs = await self.item_recommender.related(resolved_id, count)
        except Item2ItemError:
            # fallback to frequently saved for "You Might Also Like"
            recs = await self.item_recommender.frequently_saved(count=100)
        return self._sample(recs, count)

    @_empty_on_error
    async def syndicated(self, resolved_id: int, count: int) -> List[CorpusRecommendationModel]:
        try:
            recs = await self.item_recommender.syndicated(resolved_id, count)
        except Item2ItemError:
            # fallback to frequently saved syndicated for syndicated "More Stories from Pocket"
            recs = await self.item_recommender.frequently_saved(count=100, is_syndicated=True)
        return self._sample(recs, count)

    @_empty_on_error
    async def by_publisher(self, resolved_id: int, domain: str, count: int) -> List[CorpusRecommendationModel]:
        try:
            recs = await self.item_recommender.by_publisher(resolved_id, domain, count)
        except Item2ItemError:
            # fallback to random by the same publisher for syndicated right rail
            recs = await self.item_recommender.random_by_publisher(domain, count=100)
        return self._sample(recs, count)

    @staticmethod
    def _sample(recs, count):
        """ Randomly sample articles or apply thompson sampling """
        recs = random.sample(recs, count) if count < len(recs) else recs
        return [CorpusRecommendationModel(corpus_item=r) for r in recs]


class HomeDispatch:

    @inject
    def __init__(
            self,
            corpus_client: CorpusFeatureGroupClient,
            preferences_provider: UserRecommendationPreferencesProvider,
            topic_provider: TopicProvider,
            for_you_slate_provider: ForYouSlateProvider,
            recommended_reads_slate_provider: RecommendedReadsSlateProvider,
            topic_slate_providers: TopicSlateProviderFactory,
            collection_slate_provider: CollectionSlateProvider,
            pocket_hits_slate_provider: PocketHitsSlateProvider,
            life_hacks_slate_provider: LifeHacksSlateProvider,
            unleash_provider: UnleashProvider,
            user_impression_cap_provider: UserImpressionCapProvider = Provide[Container.user_impression_cap_provider],
    ):
        self.topic_provider = topic_provider
        self.corpus_client = corpus_client
        self.preferences_provider = preferences_provider
        self.user_impression_cap_provider = user_impression_cap_provider
        self.for_you_slate_provider = for_you_slate_provider
        self.recommended_reads_slate_provider = recommended_reads_slate_provider
        self.topic_slate_providers = topic_slate_providers
        self.collection_slate_provider = collection_slate_provider
        self.pocket_hits_slate_provider = pocket_hits_slate_provider
        self.life_hacks_slate_provider = life_hacks_slate_provider
        self.unleash_provider = unleash_provider

    @xray_recorder.capture_async('HomeDispatch.get_slate_lineup')
    async def get_slate_lineup(
            self, user: RequestUser, locale: LocaleModel, recommendation_count: int
    ) -> CorpusSlateLineupModel:
        if locale == LocaleModel.en_US:
            return await self.get_en_us_slate_lineup(recommendation_count=recommendation_count, user=user)
        elif locale == LocaleModel.de_DE:
            return await self.get_de_de_slate_lineup(recommendation_count=recommendation_count)
        else:
            raise ValueError(f'Invalid locale {locale}')

    @xray_recorder.capture_async('HomeDispatch.get_slate_lineup')
    async def get_en_us_slate_lineup(
            self, user: RequestUser, recommendation_count: int
    ) -> CorpusSlateLineupModel:

        """
        :param user:
        :param recommendation_count: Maximum number of recommendations to return.
        :return: Slate lineup for en-US Home:
            1. 'For You' slate if preferred topics are available, or otherwise 'Recommended Reads'
            2. Pocket Hits
            3. Collection slate
            4. 'Life Hacks' slate
            5. Topic slates according to preferred topics if available, otherwise default topics.
        """
        slates = []

        user_impression_capped_list, preferred_topics, thompson_sampling_assignment = await gather(
            self.user_impression_cap_provider.get(user),
            self._get_preferred_topics(user),
            self.unleash_provider.get_assignment('temp.web.recommendation-api.home.thompson-sampling', user=user),
        )

        enable_thompson_sampling = \
            thompson_sampling_assignment is not None and thompson_sampling_assignment.variant == 'treatment'

        if preferred_topics:
            slates += [self.for_you_slate_provider.get_slate(
                preferred_topics=preferred_topics,
                user_impression_capped_list=user_impression_capped_list,
                enable_thompson_sampling=enable_thompson_sampling,
            )]
        else:
            slates += [self.recommended_reads_slate_provider.get_slate(
                enable_thompson_sampling=enable_thompson_sampling
            )]

        slates += [
            self.pocket_hits_slate_provider.get_slate(),
            self.collection_slate_provider.get_slate(enable_thompson_sampling=enable_thompson_sampling),
            self.life_hacks_slate_provider.get_slate(),
        ]

        slates += await self._get_topic_slate_promises(preferred_topics=preferred_topics, default=DEFAULT_TOPICS)

        return CorpusSlateLineupModel(
            slates=self._dedupe_and_limit(
                slates=list(await gather(*slates)),
                recommendation_count=recommendation_count,
            ),
            recommendation_surface_id=RecommendationSurfaceId.HOME,
            experiment=thompson_sampling_assignment,
        )

    @xray_recorder.capture_async('HomeDispatch.get_slate_lineup')
    async def get_de_de_slate_lineup(self, recommendation_count: int) -> CorpusSlateLineupModel:
        """
        :param recommendation_count:
        :return: the Home slate lineup:
            1. Recommended Reads
            2. Collection slate
            3. Topic slates according to defaults
        """
        slates = [
            self.recommended_reads_slate_provider.get_slate(),
            self.collection_slate_provider.get_slate(),
        ]

        slates += await self._get_topic_slate_promises(preferred_topics=[], default=GERMAN_HOME_TOPICS)

        return CorpusSlateLineupModel(
            slates=self._dedupe_and_limit(
                slates=list(await gather(*slates)),
                recommendation_count=recommendation_count,
            ),
            recommendation_surface_id=RecommendationSurfaceId.HOME,
        )

    @staticmethod
    def _dedupe_and_limit(slates: List[CorpusSlateModel], recommendation_count: int) -> List[CorpusSlateModel]:
        """
        Deduplicate recommendations across slates, and limit the number of recommendations.
        It is assumed each individual slate consists of unique items, and this function doesn't look for items occurring
        multiple times in the same slate.
        :param slates:
        :param recommendation_count: The maximum number of recommendations for each slate.
        :return: Slates with duplicates removed and recommendation_count limit applied.
        """
        seen_corpus_ids = set()

        for slate in slates:
            slate.remove_corpus_items(seen_corpus_ids).limit(recommendation_count)
            # Add all CorpusItem ids from slate to seen_item_ids
            seen_corpus_ids |= set(slate.corpus_item_ids())

        return slates

    @xray_recorder.capture_async('HomeDispatch._get_preferred_topics')
    async def _get_preferred_topics(self, user: RequestUser) -> List[TopicModel]:
        preferences = await self.preferences_provider.fetch(str(user.user_id))
        if preferences and preferences.preferred_topics:
            return preferences.preferred_topics
        else:
            return []

    @xray_recorder.capture_async('HomeDispatch._get_topic_slate_promises')
    async def _get_topic_slate_promises(
            self,
            preferred_topics: List[TopicModel],
            default: List[str],
    ) -> List[Coroutine[Any, Any, CorpusSlateModel]]:
        """
        :param preferred_topics: List topics that the user prefers.
        :param default: List of default topic ids to fall back to, if the user has no preferred topics.
        :return: List of callables/promises that return topic slates when awaited.
        """
        preferred_topic_ids = [t.id for t in preferred_topics]
        topics = await self.topic_provider.get_topics(preferred_topic_ids or default)
        return [self.topic_slate_providers[topic].get_slate() for topic in topics]
