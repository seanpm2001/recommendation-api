from typing import List

import strawberry
from strawberry.extensions.tracing import OpenTelemetryExtension

from app.graphql.corpus_slate import CorpusSlate
from app.graphql.corpus_slate_lineup import CorpusSlateLineup
from app.graphql.item import Item
from app.graphql.resolvers.corpus_slate_lineup_resolvers import resolve_home_slate_lineup
from app.graphql.resolvers.corpus_slate_resolvers import resolve_new_tab_slate
from app.graphql.resolvers.legacy.slate_lineup_resolver import resolve_get_slate_lineup
from app.graphql.resolvers.legacy.slate_resolver import resolve_get_slate
from app.graphql.resolvers.topic_resolvers import list_topics, resolve_recommendation_preference_topics
from app.graphql.resolvers.user_recommendation_preferences_resolvers import update_user_recommendation_preferences
from app.graphql.slate import Slate
from app.graphql.slate_lineup import SlateLineup
from app.graphql.syndicated_article import SyndicatedArticle
from app.graphql.topic import Topic
from app.graphql.user import User
from app.graphql.user_recommendation_preferences import UserRecommendationPreferences


@strawberry.type
class Query:
    new_tab_slate: CorpusSlate = strawberry.field(
        resolver=resolve_new_tab_slate,
        description='Get a slate of ranked recommendations for the Firefox New Tab.'
                    ' Currently supports the Italy, France, and Spain markets.'
    )

    home_slate_lineup: CorpusSlateLineup = strawberry.field(
        resolver=resolve_home_slate_lineup,
        description='Get ranked corpus slates and recommendations to deliver a unified Home experience. '
                    # TODO: When strawberry.argument supports a default value, the following description can be moved to
                    #       the locale argument. Currently we can only have either a description or a default value.
                    #       https://github.com/strawberry-graphql/strawberry/issues/2436
                    'The locale argument determines the UI and recommendation content language.'
    )

    moz_social_slate: CorpusSlate = strawberry.field(
        resolver=resolve_new_tab_slate,
        description='----- In Development (2023-08-16) -----'
                    'Get a slate of ranked recommendations for the MozSocial.'
    )

    recommendation_preference_topics: List[Topic] = strawberry.field(
        resolver=resolve_recommendation_preference_topics,
        description='List all topics that the user can express a preference for.')

    list_topics: List[Topic] = strawberry.field(
        resolver=list_topics,
        deprecation_reason='Use `getSlateLineup` with a specific SlateLineup instead.',
        description='List all available topics that we have recommendations for.')

    get_slate: Slate = strawberry.field(
        resolver=resolve_get_slate,
        deprecation_reason='Please use queries specific to the surface ex. setMomentSlate. '
                           'If a named query for your surface does not yet exit please reach out to the '
                           'Data Products team and they will happily provide you with a named query.',
        description='Request a specific `Slate` by id')

    get_slate_lineup: SlateLineup = strawberry.field(
        resolver=resolve_get_slate_lineup,
        deprecation_reason='Please use queries specific to the surface ex. setMomentSlate. '
                           'If a named query for your surface does not yet exit please reach out to the '
                           'Data Products team and they will happily provide you with a named query.',
        description='Request a specific `SlateLineup` by id')


@strawberry.type
class Mutation:
    update_user_recommendation_preferences: UserRecommendationPreferences = strawberry.mutation(
        resolver=update_user_recommendation_preferences,
        description='Updates user preferences for content recommendations across Pocket.'
    )


schema = strawberry.federation.Schema(Query, mutation=Mutation, types=[User, SyndicatedArticle],
                                      enable_federation_2=True, extensions=[OpenTelemetryExtension])
